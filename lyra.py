# ===== lyra.py =====
# IGP link-state minimalista com métrica viva (RTT+Jitter+Hop), LSA full,
# Dijkstra e instalação de 1 next-hop por destino.
import socket, json, time, threading, subprocess, ipaddress, os, random, re
from collections import defaultdict
import heapq

# ---------- Parâmetros ----------
PORT        = 55200
HELLO_INT   = 1.0     # s
LSA_BASE    = 10.0    # s (há LSA imediato no start e no 1º HELLO)
DEAD_INT    = 3       # misses consecutivos de HELLO
ALPHA       = 0.2     # EWMA RTT
BETA        = 0.2     # EWMA Jitter
LSA_TTL     = 10
ROUTER_ID   = int(os.environ.get("RID", "1"))
LAN_PREFIXES = {1:"10.1.0.0/24", 3:"10.3.0.0/24", 5:"10.5.0.0/24"}

W_RTT, W_JIT, W_HOP = 1.0, 4.0, 1.0

# ---------- Estado ----------
neighbors  = {}                 # rid -> {ip,rtt_ms,jit_ms,missed,up}
rid_by_ip  = {}
ip_by_rid  = {}
lsdb       = defaultdict(dict)  # u -> {v: cost}
seen       = set()              # (rid, seq)
state_lock = threading.Lock()

# ---------- Utilidades ----------
def sh(cmd):
    try: return subprocess.check_output(cmd, shell=True, text=True).strip()
    except: return ""

def ip_ifaces():
    out = sh("ip -j addr")
    if out:
        try:
            data = json.loads(out); res=[]
            for itf in data:
                name = itf["ifname"]
                if name == "lo": continue
                for a in itf.get("addr_info", []):
                    if a.get("family") == "inet":
                        res.append((name, a["local"], a["prefixlen"]))
            return res
        except: pass
    res=[]
    for ln in sh("ip -o -4 addr show").splitlines():
        m = re.search(r'^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)', ln)
        if not m: continue
        ifname, ip, plen = m.group(1), m.group(2), int(m.group(3))
        if ifname == "lo": continue
        res.append((ifname, ip, plen))
    return res

def p2p_peers():
    peers=[]
    for ifname, ip, plen in ip_ifaces():
        if plen != 30: continue
        try:
            addr = ipaddress.ip_interface(f"{ip}/{plen}")
            last = int(str(addr.ip).split(".")[-1])
            if last not in (1,2): continue
            peer_last = 3 - last
            octs = str(addr.network.network_address).split(".")
            octs[-1] = str(peer_last)
            peers.append((ifname, ip, ".".join(octs)))
        except: continue
    return peers

def now_ms(): return int(time.time()*1000)

def link_cost(st):
    rtt = st.get("rtt_ms", 20.0)
    jit = st.get("jit_ms", 1.0)
    return W_RTT*rtt + W_JIT*jit + W_HOP*1.0

def dijkstra(adj, src):
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

def first_hop(prev, dest, me):
    cur=dest; p=prev.get(cur)
    if not p: return None
    while p and p!=me:
        cur=p; p=prev.get(cur)
    return cur

# ---------- Rede (socket) ----------
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("", PORT))

def flood(msg, jitter_ms=0, exclude_ip=None):
    if jitter_ms: time.sleep(jitter_ms/1000.0)
    data = json.dumps(msg).encode()
    with state_lock:
        nb = list(neighbors.items())
    for _rid, st in nb:
        ip = st.get("ip")
        if ip and ip != exclude_ip:
            try: sock.sendto(data, (ip, PORT))
            except: pass

def install_routes():
    # monta adj a partir da lsdb (simetrizando por segurança)
    with state_lock:
        adj = defaultdict(dict)
        for u, nbrs in lsdb.items():
            for v, c in nbrs.items():
                c = max(0.1, float(c))
                if c < adj[u].get(v, 1e18): adj[u][v] = c
                if c < adj[v].get(u, 1e18): adj[v][u] = c
        ip_map = dict(ip_by_rid)

    dist, prev = dijkstra(adj, ROUTER_ID)
    changed=False
    for dst_rid, pfx in LAN_PREFIXES.items():
        if dst_rid == ROUTER_ID: continue
        if dist.get(dst_rid, 1e18) >= 1e17: continue
        nh = first_hop(prev, dst_rid, ROUTER_ID)
        if nh and ip_map.get(nh):
            subprocess.call(f"ip route replace {pfx} via {ip_map[nh]}", shell=True)
            changed=True
    if changed:
        open(f"/tmp/lyra_{ROUTER_ID}.routes","w").write(sh("ip route"))

def send_full_lsa():
    # Inclui todos os vizinhos UP já medidos (após 1º HELLO)
    with state_lock:
        nbrs=[]
        for rid, st in neighbors.items():
            if st.get("up"):
                c = link_cost(st)
                nbrs.append([rid, c, now_ms()])
        msg={"t":"LSA","rid":ROUTER_ID,"seq":now_ms(),"ttl":LSA_TTL,"nbrs":nbrs}
    flood(msg, jitter_ms=random.randint(0,120))

# ---------- Threads ----------
def rx_loop():
    # Recebe pacotes e atualiza estado
    while True:
        data, addr = sock.recvfrom(65535)
        try: msg = json.loads(data.decode())
        except: continue
        typ = msg.get("t"); src_ip = addr[0]
        try: rid = int(msg.get("rid", -1))
        except: continue

        if typ in ("HELLO","SONDA"):
            first_up = False
            with state_lock:
                rid_by_ip[src_ip] = rid
                ip_by_rid[rid] = src_ip
                st = neighbors.setdefault(rid, {"ip":src_ip,"rtt_ms":20.0,"jit_ms":1.0,"missed":0,"up":False})
                rtt = max(0, now_ms() - int(msg["ts"]))
                old = st["rtt_ms"]
                st["rtt_ms"] = (1-ALPHA)*old + ALPHA*rtt
                st["jit_ms"] = (1-BETA)*st["jit_ms"] + BETA*abs(st["rtt_ms"]-old)
                st["missed"] = 0
                if not st["up"]:
                    st["up"] = True
                    first_up = True
            # Recalcula rotas em qualquer HELLO; e se for 1º HELLO desse vizinho, anuncia LSA já
            install_routes()
            if first_up:
                send_full_lsa()

        elif typ == "LSA":
            key = (rid, int(msg.get("seq",0)))
            if key in seen: 
                continue
            seen.add(key)
            with state_lock:
                nbrs = {}
                for v,c,_ts in msg.get("nbrs", []):
                    nbrs[int(v)] = float(c)
                lsdb[rid] = nbrs
            ttl = int(msg.get("ttl",1))
            if ttl > 0:
                msg["ttl"] = ttl - 1
                flood(msg, jitter_ms=random.randint(0,120), exclude_ip=src_ip)
            install_routes()

def send_hello():
    # Envia HELLO periódico para cada par /30
    while True:
        for _ifn, myip, peer in p2p_peers():
            msg={"t":"HELLO","rid":ROUTER_ID,"ts":now_ms(),"ip":myip}
            try: sock.sendto(json.dumps(msg).encode(), (peer, PORT))
            except: pass
        time.sleep(HELLO_INT)

def liveness_monitor():
    # Marca vizinho como down após DEAD_INT misses e anuncia LSA
    while True:
        time.sleep(HELLO_INT)
        fallen=False
        with state_lock:
            for rid, st in neighbors.items():
                st["missed"] = st.get("missed",0) + 1
                if st["missed"] >= DEAD_INT and st.get("up",True):
                    st["up"] = False
                    fallen=True
        if fallen:
            send_full_lsa()
            install_routes()

def periodic_lsa():
    # LSA periódico (backup de sincronização)
    while True:
        time.sleep(LSA_BASE)
        send_full_lsa()

def bootstrap_lsa():
    # LSA imediato no start, após uma curtíssima espera para o kernel subir as ifaces
    time.sleep(0.5)
    send_full_lsa()

def main():
    threading.Thread(target=rx_loop,          daemon=True).start()
    threading.Thread(target=send_hello,       daemon=True).start()
    threading.Thread(target=liveness_monitor, daemon=True).start()
    threading.Thread(target=periodic_lsa,     daemon=True).start()
    threading.Thread(target=bootstrap_lsa,    daemon=True).start()
    # Loop principal apenas mantém o processo vivo
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main() 