#!/usr/bin/env python3
# lyra.py — LYRA-FE (link-state métrica viva + softmin/ECMP)
# Compatível com Python 3.6 (usa universal_newlines) e com VMs/Mininet (HELLO unicast em /30).

import socket, json, time, threading, subprocess, ipaddress, os, random, re
from collections import defaultdict

# ---------------- Parâmetros ----------------
PORT = 55200
HELLO_INT = 1.0                # período HELLO (s)
SONDA_INT = 8.0                # período base das SONDAS (s)
LSA_BASE = 10.0                # LSA full (s)
DELTA_THRESH_MS = 2.0          # limiar p/ LSA delta (ms)
DEAD_INT = 3                   # HELLOs perdidos para considerar down
ALPHA = 0.2                    # EWMA RTT
BETA  = 0.2                    # EWMA jitter
HYST_MS = 5.0                  # (conceitual) troca se ganho >= 5ms
LSA_TTL = 10                   # TTL para flooding
TAU = 6.0                      # temperatura do softmin
EPS = 3.0                      # janela de quase-ótimos (ms)

# pesos da métrica: c = RTT + 4*Jit + 8*Perda(%) + 1*Hop
W_RTT=1.0; W_JIT=4.0; W_LOSS=8.0; W_HOP=1.0

ROUTER_ID = int(os.environ.get("RID", "1"))
LAN_PREFIXES = {1:"10.1.0.0/24", 3:"10.3.0.0/24", 5:"10.5.0.0/24"}

# ---------------- Utilidades ----------------
def sh(cmd):
    """Executa shell e retorna stdout (compatível Py3.6)."""
    try:
        return subprocess.check_output(cmd, shell=True, universal_newlines=True).strip()
    except subprocess.CalledProcessError:
        return ""

def ip_ifaces():
    """
    [(ifname, ip, prefixlen, broadcast)] para IPv4 (exceto lo).
    Tenta 'ip -j addr' e cai para 'ip -o -4 addr show' se necessário.
    """
    out = sh("ip -j addr")
    if out:
        try:
            data = json.loads(out); res=[]
            for itf in data:
                name = itf["ifname"]
                if name == "lo": continue
                for a in itf.get("addr_info", []):
                    if a.get("family") == "inet":
                        ip = a["local"]; plen = a["prefixlen"]
                        net = ipaddress.ip_interface("{}/{}".format(ip, plen)).network
                        bcast = a.get("broadcast") or str(net.broadcast_address)
                        res.append((name, ip, plen, bcast))
            return res
        except Exception:
            pass
    # fallback (18.04)
    res=[]
    for ln in sh("ip -o -4 addr show").splitlines():
        m = re.search(r'^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)(?:\s+brd\s+(\d+\.\d+\.\d+\.\d+))?', ln)
        if not m: continue
        ifname, ip, plen, bcast = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if ifname == "lo": continue
        if not bcast:
            net = ipaddress.ip_interface("{}/{}".format(ip, plen)).network
            bcast = str(net.broadcast_address)
        res.append((ifname, ip, plen, bcast))
    return res

def p2p_peers():
    """
    Descobre vizinhos ponto-a-ponto em /30.
    ip x.y.z.(1|2)/30 -> par é .2 se sou .1, e vice-versa.
    Retorna [(ifname, my_ip, peer_ip)].
    """
    peers=[]
    for ifname, ip, plen, _b in ip_ifaces():
        if plen != 30:  # só enlaces P2P /30
            continue
        try:
            addr = ipaddress.ip_interface("{}/{}".format(ip, plen))
            last = int(str(addr.ip).split(".")[-1])
            if last not in (1,2):  # esperamos .1 ou .2
                continue
            peer_last = 3 - last   # 1->2, 2->1
            octs = str(addr.network.network_address).split(".")
            octs[-1] = str(peer_last)
            peer_ip = ".".join(octs)
            peers.append((ifname, ip, peer_ip))
        except Exception:
            continue
    return peers

def now_ms(): return int(time.time()*1000)

# ---------------- Socket & estado ----------------
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("", PORT))

neighbors = {}                     # rid -> {ip,rtt_ms,jit_ms,loss,missed,up}
rid_by_ip, ip_by_rid = {}, {}
lsdb = defaultdict(dict)           # origem -> {vizinho: custo}
last_adv = {}                      # último custo anunciado
seen = set()                       # LSAs vistos
lock = threading.Lock()

# ---------------- Métrica e grafo ----------------
def link_cost(st):
    rtt = st.get("rtt_ms", 20.0)
    jit = st.get("jit_ms", 1.0)
    loss = 100.0 * st.get("loss", 0.0)  # transforma em %
    return W_RTT*rtt + W_JIT*jit + W_LOSS*loss + W_HOP*1.0

def flood(msg, jitter_ms=0):
    time.sleep(jitter_ms/1000.0)
    data = json.dumps(msg).encode()
    for _rid, st in neighbors.items():
        ip = st.get("ip")
        if ip:
            try: sock.sendto(data, (ip, PORT))
            except: pass

def build_adj():
    adj = defaultdict(dict)
    for u, nbrs in lsdb.items():
        for v, c in nbrs.items():
            c = max(0.1, float(c))
            adj[u][v] = min(adj[u].get(v, 1e9), c)
            adj[v][u] = min(adj[v].get(u, 1e9), c)
    return adj

def dijkstra(adj, src):
    import heapq
    INF=1e18; dist=defaultdict(lambda: INF); prev={}
    dist[src]=0; pq=[(0,src)]
    while pq:
        d,u=heapq.heappop(pq)
        if d!=dist[u]: continue
        for v,c in adj[u].items():
            nd=d+c
            if nd<dist[v]-1e-9:
                dist[v]=nd; prev[v]=u; heapq.heappush(pq,(nd,v))
    return dist, prev

def first_hop(prev, dest):
    cur=dest; p=prev.get(cur)
    if not p: return None
    while p and p!=ROUTER_ID:
        cur=p; p=prev.get(cur)
    return cur

# ---------------- Instalação de rotas ----------------
def install_routes_softmin():
    adj=build_adj()
    dist, prev = dijkstra(adj, ROUTER_ID)
    changed=False

    for dst_rid, pfx in LAN_PREFIXES.items():
        if dst_rid==ROUTER_ID: continue
        if dist.get(dst_rid,1e18)>=1e17: continue

        # candidatos: vizinhos j com Sij <= melhor + EPS
        cands=[]
        for j, cij in adj[ROUTER_ID].items():
            Sj = dist.get(j,1e18) + cij
            if Sj <= dist[dst_rid] + EPS:
                cands.append((j, Sj))

        if not cands:
            nh = first_hop(prev, dst_rid)
            if nh and ip_by_rid.get(nh):
                subprocess.call("ip route replace {} via {}".format(pfx, ip_by_rid[nh]), shell=True)
                changed=True
            continue

        cands.sort(key=lambda x: x[1])
        best = cands[0]
        second = cands[1] if len(cands)>1 else None

        import math
        Z = sum(math.exp(-(s)/TAU) for (_j,s) in cands[:2])
        weights=[]
        for (ridj,s) in ([best]+([second] if second else [])):
            w = max(1, int(100*math.exp(-s/TAU)/max(1e-9,Z)))  # 1..100
            weights.append((ridj,w))

        if len(weights)==1:
            nh, _w = weights[0]
            ip1 = ip_by_rid.get(nh)
            if ip1:
                subprocess.call("ip route replace {} via {}".format(pfx, ip1), shell=True)
                changed=True
        else:
            (nh1,w1),(nh2,w2) = weights[0], weights[1]
            ip1,ip2 = ip_by_rid.get(nh1), ip_by_rid.get(nh2)
            if ip1 and ip2:
                cmd = ("ip route replace {} "
                       "nexthop via {} weight {} "
                       "nexthop via {} weight {}").format(pfx, ip1, w1, ip2, w2)
                subprocess.call(cmd, shell=True)
                changed=True
            elif ip1:
                subprocess.call("ip route replace {} via {}".format(pfx, ip1), shell=True)
                changed=True

    if changed:
        open("/tmp/lyra_{}.routes".format(ROUTER_ID), "w").write(sh("ip route"))

# ---------------- LSA (delta/full) ----------------
def announce_delta_if_needed():
    with lock:
        changed=[]
        for rid, st in neighbors.items():
            if not st.get("up"): continue
            c = link_cost(st)
            prev = last_adv.get(rid)
            if prev is None or abs(c-prev) >= DELTA_THRESH_MS:
                last_adv[rid]=c; changed.append([rid, c, now_ms()])
        if changed:
            msg={"t":"LSA","rid":ROUTER_ID,"seq":int(time.time()*1000)%2**31,
                 "ttl":LSA_TTL,"delta":True,"nbrs":changed}
            flood(msg, jitter_ms=random.randint(0,300))

def send_full_lsa():
    while True:
        time.sleep(LSA_BASE)
        with lock:
            nbrs=[]
            for rid, st in neighbors.items():
                if st.get("up"):
                    c=link_cost(st); last_adv[rid]=c
                    nbrs.append([rid,c,now_ms()])
            msg={"t":"LSA","rid":ROUTER_ID,"seq":int(time.time())%2**31,
                 "ttl":LSA_TTL,"delta":False,"nbrs":nbrs}
        flood(msg, jitter_ms=random.randint(0,300))

# ---------------- HELLO/SONDA ----------------
def send_hello():
    """
    Envia HELLO **unicast** para cada vizinho /30 (mais robusto em VM/Mininet).
    """
    while True:
        for _ifn, myip, peer in p2p_peers():
            msg = {"t":"HELLO","rid":ROUTER_ID,"ts":now_ms(),"ip":myip}
            try:
                sock.setsockopt(socket.SOL_IP, socket.IP_TTL, 1)  # TTL=1
                sock.sendto(json.dumps(msg).encode(), (peer, PORT))
            except Exception:
                pass
        time.sleep(HELLO_INT)

def send_sondas():
    while True:
        time.sleep(max(1.0, SONDA_INT + random.uniform(-2,2)))
        with lock:
            ups=[(rid,st.get("ip")) for rid,st in neighbors.items() if st.get("up") and st.get("ip")]
        if not ups:
            # bootstrap via pares /30 se ainda não conhece vizinhos
            ups=[(None, peer) for (_i,_m,peer) in p2p_peers()]
        if not ups: 
            continue
        _rid, ip = random.choice(ups)
        for k in range(3):
            msg={"t":"SONDA","rid":ROUTER_ID,"ts":now_ms(),"k":k}
            try: sock.sendto(json.dumps(msg).encode(), (ip, PORT))
            except Exception: pass
            time.sleep(0.15)

# ---------------- RX + Liveness ----------------
def rx_loop():
    while True:
        data, addr = sock.recvfrom(65535)
        try: msg = json.loads(data.decode())
        except Exception: continue
        typ = msg.get("t"); src = addr[0]; t=now_ms()

        if typ in ("HELLO","SONDA"):
            rid=int(msg["rid"]); ts=int(msg["ts"])
            rtt=max(0, t-ts)
            with lock:
                rid_by_ip[src]=rid; ip_by_rid[rid]=src
                st = neighbors.setdefault(rid, {"ip":src,"rtt_ms":rtt,"jit_ms":0.0,"loss":0.0,"missed":0,"up":True})
                old = st["rtt_ms"]
                st["rtt_ms"] = (1-ALPHA)*old + ALPHA*rtt
                st["jit_ms"] = (1-BETA)*st["jit_ms"] + BETA*abs(st["rtt_ms"]-old)
                st["missed"]=0; st["up"]=True
                cnt = st.get("cnt", {"exp":0,"rcv":0}); cnt["exp"]+=1; cnt["rcv"]+=1; st["cnt"]=cnt
                st["loss"] = max(0.0, 1.0 - (cnt["rcv"]/max(1,cnt["exp"])))
            announce_delta_if_needed()

        elif typ=="LSA":
            key=(int(msg["rid"]), int(msg["seq"]))
            if key in seen: continue
            seen.add(key)
            srcRID = int(msg["rid"])
            with lock:
                if msg.get("delta",False):
                    cur = lsdb[srcRID]
                    for v,c,ts in msg.get("nbrs", []):
                        cur[int(v)] = float(c)
                else:
                    lsdb[srcRID] = { int(v): float(c) for v,c,ts in msg.get("nbrs", []) }
            ttl=int(msg.get("ttl",1))
            if ttl>0:
                msg["ttl"]=ttl-1
                flood(msg, jitter_ms=random.randint(0,150))
            install_routes_softmin()

def liveness():
    while True:
        time.sleep(HELLO_INT)
        with lock:
            for rid, st in list(neighbors.items()):
                st["missed"]=st.get("missed",0)+1
                cnt = st.get("cnt", {"exp":0,"rcv":0}); cnt["exp"]+=1; st["cnt"]=cnt
                st["loss"] = max(0.0, 1.0 - (cnt["rcv"]/max(1,cnt["exp"])))
                st["rcv"]=0
                if st["missed"]>=DEAD_INT and st.get("up",True):
                    st["up"]=False
                    if ROUTER_ID in lsdb and rid in lsdb[ROUTER_ID]:
                        del lsdb[ROUTER_ID][rid]
                    announce_delta_if_needed()

# ---------------- Bootstrap ----------------
if __name__=="__main__":
    threading.Thread(target=send_hello,   daemon=True).start()
    threading.Thread(target=send_sondas,  daemon=True).start()
    threading.Thread(target=send_full_lsa,daemon=True).start()
    threading.Thread(target=rx_loop,      daemon=True).start()
    threading.Thread(target=liveness,     daemon=True).start()
    while True: time.sleep(10)
