#!/usr/bin/env python3
import socket, json, time, threading, subprocess, ipaddress, os, random
from collections import defaultdict

PORT = 55200
HELLO_INT = 1.0
SONDA_INT = 8.0               # rajadas para refinar jitter/perda
LSA_BASE = 10.0               # full LSA
DELTA_THRESH_MS = 2.0         # sensibilidade p/ LSA delta
DEAD_INT = 3
ALPHA = 0.2                   # EWMA RTT
BETA  = 0.2                   # EWMA jitter
HYST_MS = 5.0
LSA_TTL = 10
TAU = 6.0                     # temperatura para softmin/ECMP
EPS = 3.0                     # janela de quase-ótimos em ms

W_RTT=1.0; W_JIT=4.0; W_LOSS=8.0; W_HOP=1.0

ROUTER_ID = int(os.environ.get("RID", "1"))
LAN_PREFIXES = {1:"10.1.0.0/24", 3:"10.3.0.0/24", 5:"10.5.0.0/24"}

def sh(cmd):
    try: return subprocess.check_output(cmd, shell=True, text=True).strip()
    except subprocess.CalledProcessError: return ""

def ip_ifaces():
    data = json.loads(sh("ip -j addr")); out=[]
    for itf in data:
        name = itf["ifname"];
        if name=="lo": continue
        for a in itf.get("addr_info", []):
            if a["family"]=="inet":
                ip=a["local"]; plen=a["prefixlen"]
                net=ipaddress.ip_interface(f"{ip}/{plen}").network
                out.append((name, ip, plen, str(net.broadcast_address)))
    return out

def now_ms(): return int(time.time()*1000)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("", PORT))

neighbors = {}   # rid -> {ip,rtt_ms,jit_ms,loss,missed,up,last_cost}
rid_by_ip, ip_by_rid = {}, {}
lsdb = defaultdict(dict)   # lsdb[srcRID][nbrRID] = cost
last_adv = {}              # ultimo custo anunciado para meus vizinhos
seen = set()
lock = threading.Lock()

def link_cost(st):
    rtt = st.get("rtt_ms", 20.0)
    jit = st.get("jit_ms", 1.0)
    loss = 100.0*st.get("loss", 0.0) # %
    hop = 1.0
    return W_RTT*rtt + W_JIT*jit + W_LOSS*loss + W_HOP*hop

def flood(msg, jitter_ms=0):
    time.sleep(jitter_ms/1000.0)
    data = json.dumps(msg).encode()
    for rid, st in neighbors.items():
        if st.get("ip"):
            try: sock.sendto(data, (st["ip"], PORT))
            except: pass

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

def build_adj():
    adj=defaultdict(dict)
    for u,nbrs in lsdb.items():
        for v,c in nbrs.items():
            c=max(0.1, float(c))
            adj[u][v]=min(adj[u].get(v,1e9), c)
            adj[v][u]=min(adj[v].get(u,1e9), c)
    return adj

def first_hop(prev, dest):
    cur=dest; p=prev.get(cur)
    if not p: return None
    while p and p!=ROUTER_ID:
        cur=p; p=prev.get(cur)
    return cur

def install_routes_softmin():
    adj=build_adj()
    dist, prev = dijkstra(adj, ROUTER_ID)
    changed=False

    for dst_rid, pfx in LAN_PREFIXES.items():
        if dst_rid==ROUTER_ID: continue
        if dist.get(dst_rid,1e18)>=1e17: continue

        # candidatos: vizinhos j de ROUTER_ID
        cands=[]
        for j, cij in adj[ROUTER_ID].items():
            Sj = dist.get(j,1e18) + cij
            if Sj <= dist[dst_rid] + EPS:
                cands.append((j, Sj))
        if not cands:
            # fallback: melhor hop
            nh = first_hop(prev, dst_rid)
            if nh and ip_by_rid.get(nh):
                subprocess.call(f"ip route replace {pfx} via {ip_by_rid[nh]}", shell=True)
                changed=True
            continue

        # ordena por score
        cands.sort(key=lambda x: x[1])
        best = cands[0]
        second = cands[1] if len(cands)>1 else None

        # probabilidades estilo Gibbs
        import math
        Z = sum(math.exp(-(s)/TAU) for (_,s) in cands[:2])
        weights=[]
        for (ridj,s) in ([best]+([second] if second else [])):
            w = max(1, int(100*math.exp(-s/TAU)/max(1e-9,Z)))  # 1..100
            weights.append((ridj,w))

        # instala uma ou duas nexthops (com weight se suportado)
        if len(weights)==1:
            nh, w = weights[0]
            nh_ip = ip_by_rid.get(nh)
            if nh_ip:
                subprocess.call(f"ip route replace {pfx} via {nh_ip}", shell=True)
                changed=True
        else:
            nh1,w1 = weights[0]; nh2,w2 = weights[1]
            ip1,ip2 = ip_by_rid.get(nh1), ip_by_rid.get(nh2)
            if ip1 and ip2:
                # rota multipath com pesos
                cmd = f"ip route replace {pfx} " \
                      f"nexthop via {ip1} weight {w1} " \
                      f"nexthop via {ip2} weight {w2}"
                subprocess.call(cmd, shell=True)
                changed=True
            elif ip1:
                subprocess.call(f"ip route replace {pfx} via {ip1}", shell=True)
                changed=True

    if changed:
        open(f"/tmp/lyra_{ROUTER_ID}.routes","w").write(sh("ip route"))

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

def send_hello():
    while True:
        for itf, ip, plen, bcast in ip_ifaces():
            msg={"t":"HELLO","rid":ROUTER_ID,"ts":now_ms(),"ip":ip}
            sock.setsockopt(socket.SOL_IP, socket.IP_TTL, 1)
            try: sock.sendto(json.dumps(msg).encode(), (bcast, PORT))
            except: pass
        time.sleep(HELLO_INT)

def send_sondas():
    while True:
        time.sleep(max(1.0, SONDA_INT + random.uniform(-2,2)))
        with lock:
            ups=[(rid,st.get("ip")) for rid,st in neighbors.items() if st.get("up") and st.get("ip")]
        if not ups: continue
        rid, ip = random.choice(ups)
        for k in range(3):
            msg={"t":"SONDA","rid":ROUTER_ID,"ts":now_ms(),"k":k}
            try: sock.sendto(json.dumps(msg).encode(), (ip, PORT))
            except: pass
            time.sleep(0.15)

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

def rx_loop():
    while True:
        data, addr = sock.recvfrom(65535)
        try: msg = json.loads(data.decode())
        except: continue
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

if __name__=="__main__":
    threading.Thread(target=send_hello,   daemon=True).start()
    threading.Thread(target=send_sondas,  daemon=True).start()
    threading.Thread(target=send_full_lsa,daemon=True).start()
    threading.Thread(target=rx_loop,      daemon=True).start()
    threading.Thread(target=liveness,     daemon=True).start()
    while True: time.sleep(10)