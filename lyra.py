# lyra.py — LYRA-FE (link-state métrica viva + softmin/ECMP)
# REESTRUTURADO COM ARQUITETURA DE FILA DE EVENTOS PARA ELIMINAR RACE CONDITIONS

import socket, json, time, threading, subprocess, ipaddress, os, random, re
from collections import defaultdict
from queue import Queue
import math
import heapq

# ---------------- Parâmetros ----------------
# (Sem alterações nesta seção)
PORT = 55200
HELLO_INT = 1.0
SONDA_INT = 8.0
LSA_BASE = 10.0
DELTA_THRESH_MS = 2.0
DEAD_INT = 3
ALPHA = 0.2
BETA  = 0.2
LSA_TTL = 10
TAU = 6.0
EPS = 3.0
W_RTT=1.0; W_JIT=4.0; W_LOSS=8.0; W_HOP=1.0
ROUTER_ID = int(os.environ.get("RID", "1"))
LAN_PREFIXES = {1:"10.1.0.0/24", 3:"10.3.0.0/24", 5:"10.5.0.0/24"}

# ---------------- Estado Global ----------------
# O estado agora é encapsulado para clareza
class GlobalState:
    def __init__(self):
        self.neighbors = {}             # rid -> {ip,rtt_ms,jit_ms,loss,missed,up}
        self.rid_by_ip = {}
        self.ip_by_rid = {}
        self.lsdb = defaultdict(dict)   # origem -> {vizinho: custo}
        self.last_adv = {}              # último custo anunciado
        self.seen = set()               # LSAs vistos
        self.lock = threading.Lock()    # Lock para acesso concorrente ao estado

# Instância única do estado e da fila de eventos
state = GlobalState()
event_queue = Queue()

# ---------------- Funções de Utilidade ----------------
# (Sem alterações significativas, exceto por não dependerem mais de globais soltas)
def sh(cmd):
    try: return subprocess.check_output(cmd, shell=True, universal_newlines=True).strip()
    except: return ""

def ip_ifaces():
    # ... (código original sem alterações) ...
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
                        res.append((name, ip, plen))
            return res
        except Exception: pass
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
            peer_ip = ".".join(octs)
            peers.append((ifname, ip, peer_ip))
        except Exception: continue
    return peers

def now_ms(): return int(time.time()*1000)

# ---------------- Lógica de Rede (Funções Puras) ----------------
# Estas funções operam sobre o estado, mas não o modificam diretamente
def link_cost(st):
    rtt = st.get("rtt_ms", 20.0); jit = st.get("jit_ms", 1.0)
    loss = 100.0 * st.get("loss", 0.0)
    return W_RTT*rtt + W_JIT*jit + W_LOSS*loss + W_HOP*1.0

def dijkstra(adj, src):
    INF=1e18; dist=defaultdict(lambda: INF); prev={}
    dist[src]=0; pq=[(0,src)]
    while pq:
        d,u=heapq.heappop(pq)
        if d!=dist[u]: continue
        for v,c in adj[u].items():
            nd=d+c
            if nd<dist[v]-1e-9: dist[v]=nd; prev[v]=u; heapq.heappush(pq,(nd,v))
    return dist, prev

def first_hop(prev, dest):
    cur=dest; p=prev.get(cur)
    if not p: return None
    while p and p!=ROUTER_ID: cur=p; p=prev.get(cur)
    return cur

# ---------------- Threads Produtoras de Eventos ----------------
# Estas threads apenas geram eventos e os colocam na fila.

def rx_loop(sock, queue):
    """Recebe pacotes e os coloca na fila de eventos como mensagens."""
    while True:
        data, addr = sock.recvfrom(65535)
        try:
            msg = json.loads(data.decode())
            event = {"type": "packet_received", "addr": addr[0], "msg": msg}
            queue.put(event)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass # Ignora pacotes malformados

def liveness_monitor(queue):
    """Periodicamente, verifica a atividade dos vizinhos e enfileira eventos de timeout."""
    while True:
        time.sleep(HELLO_INT)
        event = {"type": "liveness_check"}
        queue.put(event)

def periodic_lsa_sender(queue):
    """Periodicamente, enfileira um evento para enviar um LSA completo."""
    while True:
        time.sleep(LSA_BASE)
        event = {"type": "send_full_lsa"}
        queue.put(event)

# ---------------- O "Engine" Central ----------------
# Esta é a única thread que modifica o estado e toma decisões.

def engine_loop(sock, queue, state):
    """Processa eventos da fila sequencialmente."""
    
    def flood(msg, jitter_ms=0, exclude_ip=None):
        time.sleep(jitter_ms/1000.0)
        data = json.dumps(msg).encode()
        # Acessa a lista de vizinhos de forma segura
        with state.lock:
            neighbors_list = list(state.neighbors.items())
        
        for _rid, st in neighbors_list:
            ip = st.get("ip")
            if ip and ip != exclude_ip:
                try: sock.sendto(data, (ip, PORT))
                except: pass
    
    def calculate_and_install_routes():
        with state.lock:
            # Cria a lista de adjacências a partir do LSDB atual
            adj = defaultdict(dict)
            for u, nbrs in state.lsdb.items():
                for v, c in nbrs.items():
                    c = max(0.1, float(c))
                    adj[u][v] = min(adj[u].get(v, 1e9), c)
                    adj[v][u] = min(adj[v].get(u, 1e9), c)
            
            # Copia o dicionário ip_by_rid para uso seguro fora do lock
            ip_by_rid_copy = state.ip_by_rid.copy()

        dist, prev = dijkstra(adj, ROUTER_ID)
        changed = False

        for dst_rid, pfx in LAN_PREFIXES.items():
            if dst_rid == ROUTER_ID or dist.get(dst_rid, 1e18) >= 1e17:
                continue

            nh = first_hop(prev, dst_rid)
            if nh and ip_by_rid_copy.get(nh):
                cmd = f"ip route replace {pfx} via {ip_by_rid_copy[nh]}"
                subprocess.call(cmd, shell=True)
                changed = True
        
        if changed:
            open(f"/tmp/lyra_{ROUTER_ID}.routes", "w").write(sh("ip route"))

    while True:
        event = queue.get() # Pega o próximo evento da fila
        evt_type = event.get("type")

        if evt_type == "packet_received":
            msg, src_ip = event["msg"], event["addr"]
            typ, rid = msg.get("t"), msg.get("rid")
            
            if typ in ("HELLO", "SONDA"):
                with state.lock:
                    rid = int(rid)
                    state.rid_by_ip[src_ip] = rid
                    state.ip_by_rid[rid] = src_ip
                    st = state.neighbors.setdefault(rid, {"ip":src_ip, "rtt_ms":0.0, "jit_ms":0.0, "loss":0.0})
                    
                    rtt = max(0, now_ms() - int(msg["ts"]))
                    old_rtt = st.get("rtt_ms", rtt)
                    st["rtt_ms"] = (1-ALPHA)*old_rtt + ALPHA*rtt
                    st["jit_ms"] = (1-BETA)*st.get("jit_ms",0) + BETA*abs(st["rtt_ms"]-old_rtt)
                    st["missed"] = 0
                    st["up"] = True
                calculate_and_install_routes()

            elif typ == "LSA":
                key = (int(rid), int(msg["seq"]))
                if key in state.seen: continue
                state.seen.add(key)
                srcRID = int(rid)
                
                with state.lock:
                    if not msg.get("delta", False):
                        state.lsdb[srcRID] = {int(v): float(c) for v, c, ts in msg.get("nbrs", [])}
                    else: # Delta LSA
                        for v, c, ts in msg.get("nbrs", []):
                            state.lsdb[srcRID][int(v)] = float(c)

                ttl = int(msg.get("ttl", 1))
                if ttl > 0:
                    msg["ttl"] = ttl - 1
                    flood(msg, jitter_ms=random.randint(0,150), exclude_ip=src_ip)
                
                calculate_and_install_routes()

        elif evt_type == "liveness_check":
            recalc_needed = False
            fallen_neighbors = []
            with state.lock:
                for rid, st in state.neighbors.items():
                    st["missed"] = st.get("missed", 0) + 1
                    if st["missed"] >= DEAD_INT and st.get("up", True):
                        st["up"] = False
                        recalc_needed = True
                        fallen_neighbors.append(rid)
            
            if recalc_needed:
                print(f"!!! R{ROUTER_ID} detectou que {fallen_neighbors} cairam ou voltaram !!! Recalculando e anunciando.")
                # Envia um LSA completo para notificar a rede sobre a mudança de topologia
                event_queue.put({"type": "send_full_lsa"})
                calculate_and_install_routes()

        elif evt_type == "send_full_lsa":
            with state.lock:
                nbrs = []
                for rid, st in state.neighbors.items():
                    if st.get("up"):
                        c = link_cost(st)
                        state.last_adv[rid] = c
                        nbrs.append([rid, c, now_ms()])
                msg = {"t":"LSA", "rid":ROUTER_ID, "seq":now_ms(), "ttl":LSA_TTL, "nbrs":nbrs}
            flood(msg, jitter_ms=random.randint(0,300))

# ---------------- Threads Produtoras de HELLO/SONDA ----------------
# Estas não usam a fila principal, pois apenas enviam pacotes para fora.

def send_hello(sock):
    while True:
        for _ifn, myip, peer in p2p_peers():
            msg = {"t":"HELLO", "rid":ROUTER_ID, "ts":now_ms(), "ip":myip}
            try:
                sock.sendto(json.dumps(msg).encode(), (peer, PORT))
            except Exception: pass
        time.sleep(HELLO_INT)

def send_sondas(sock):
    while True:
        time.sleep(max(1.0, SONDA_INT + random.uniform(-2,2)))
        with state.lock:
            # Escolhe um vizinho ativo para sondar
            ups = [(rid, st.get("ip")) for rid, st in state.neighbors.items() if st.get("up") and st.get("ip")]
        if not ups: continue
        
        _rid, ip = random.choice(ups)
        for k in range(3):
            msg={"t":"SONDA", "rid":ROUTER_ID, "ts":now_ms(), "k":k}
            try: sock.sendto(json.dumps(msg).encode(), (ip, PORT))
            except Exception: pass
            time.sleep(0.15)

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", PORT))

    # Inicia as threads produtoras de eventos/pacotes
    threading.Thread(target=rx_loop, args=(sock, event_queue), daemon=True).start()
    threading.Thread(target=liveness_monitor, args=(event_queue,), daemon=True).start()
    threading.Thread(target=periodic_lsa_sender, args=(event_queue,), daemon=True).start()
    threading.Thread(target=send_hello, args=(sock,), daemon=True).start()
    threading.Thread(target=send_sondas, args=(sock,), daemon=True).start()

    # Inicia a thread principal do Engine
    engine = threading.Thread(target=engine_loop, args=(sock, event_queue, state), daemon=True)
    engine.start()

    # Mantém o programa principal vivo
    engine.join()

if __name__ == "__main__":
    main()