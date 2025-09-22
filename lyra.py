# ===== lyra.py =====
# IGP link-state minimalista com métrica viva (RTT+Jitter+Hop), LSA full,
# Dijkstra e instalação de 1 next-hop por destino.
#
# OBJETIVO
# Este programa faz com que "roteadores" virtuais (processos Python rodando em cada
# r1..r5 do Mininet) descubram automaticamente os melhores caminhos para chegar às
# redes dos outros roteadores. Eles:
#   1) Medem o "tempo de ida e volta" (RTT) até seus vizinhos.
#   2) Calculam um "custo" de cada enlace (link) com base nesse RTT e na variação dele (Jitter).
#   3) Enviam mensagens com seus custos para toda a rede (LSA).
#   4) Montam um "mapa" (grafo) de quem está ligado em quem e com qual custo.
#   5) Roda o algoritmo de Dijkstra no mapa para achar os melhores caminhos.
#   6) Instala as rotas no Linux com o comando `ip route`.
#
# Como os nós se acham?
# - Cada par de roteadores está conectado por um "link /30" (uma mini-rede com 2 IPs válidos).
# - O script calcula automaticamente o IP do "vizinho" olhando o /30 e deduzindo o outro lado.
#
# Como conversam?
# - Enviam pacotes UDP (porta 55200) com mensagens em JSON:
#     * HELLO/SONDA: para medir RTT/jitter e saber se o vizinho está vivo.
#     * LSA: para "divulgar" seus custos de enlaces para toda a rede.
#
# Termos-chave:
# - RTT: tempo de ida e volta até o vizinho (em milissegundos).
# - Jitter: quão instável está o RTT (variação). Usamos uma média móvel EWMA.
# - LSA (Link-State Advertisement): anúncio do estado dos enlaces de um roteador.
# - LSDB (Link-State DataBase): base com todos os LSA recebidos (o tal "mapa" global).
# - Dijkstra: algoritmo clássico para achar o caminho de menor custo em um grafo.

import socket, json, time, threading, subprocess, ipaddress, os, random, re
from collections import defaultdict
import heapq

# ---------- Parâmetros ----------
# Porta UDP usada para todas as mensagens do protocolo.
PORT        = 55200
# Intervalo entre HELLOs (em segundos). Mantemos curto para detectar rápido vizinhos "down".
HELLO_INT   = 1.0
# Período do LSA periódico (backup de sincronização). Além deste, há LSAs imediatos em eventos.
LSA_BASE    = 10.0
# Quantas iterações de HELLO sem resposta até considerar o vizinho "down".
DEAD_INT    = 3
# Parâmetros de suavização (EWMA) para RTT e Jitter. Valores entre 0 e 1.
ALPHA       = 0.2     # quanto maior, mais "rápido" reage o RTT
BETA        = 0.2     # quanto maior, mais "rápido" reage o Jitter
# TTL (quantos saltos) que um LSA pode percorrer antes de parar de ser retransmitido.
LSA_TTL     = 10
# Identificador único do roteador (1..5 no nosso laboratório). Vem da variável de ambiente RID.
ROUTER_ID   = int(os.environ.get("RID", "1"))
# Redes "finais" (LANs) que cada roteador anuncia/possui. Usamos isso para instalar as rotas.
LAN_PREFIXES = {1:"10.1.0.0/24", 3:"10.3.0.0/24", 5:"10.5.0.0/24"}

# Pesos usados para compor o custo do enlace: custo = 1*RTT + 4*Jitter + 1*Hop
# (Hop é um "1 fixo" para preferir caminhos com menos saltos quando empata).
W_RTT, W_JIT, W_HOP = 1.0, 4.0, 1.0

# ---------- Estado ----------
# neighbors: informações por vizinho (métricas e status).
neighbors  = {}                 # rid -> {ip,rtt_ms,jit_ms,missed,up}
# Mapas para traduzir IP <-> RID (ajuda a instalar rotas e a construir a topologia).
rid_by_ip  = {}
ip_by_rid  = {}
# lsdb: base de estado dos enlaces (o grafo): para um nó u, guardamos {v: custo(u->v)}.
lsdb       = defaultdict(dict)  # u -> {v: cost}
# seen: conjunto de LSAs já vistos (para não reenviar/processar duplicado).
seen       = set()              # (rid, seq)
# Trava para acessos concorrentes ao estado (várias threads).
state_lock = threading.Lock()

# ---------- Utilidades ----------
def sh(cmd):
    """
    Executa um comando de shell e retorna a saída como string.
    Usamos para consultar `ip route`, `ip addr` etc.
    """
    try: return subprocess.check_output(cmd, shell=True, text=True).strip()
    except: return ""

def ip_ifaces():
    """
    Lista as interfaces IPv4 ativas (exceto 'lo'), retornando pares (nome, ip, prefixlen).
    Tentamos parsear JSON via `ip -j addr`; se não der, caímos no formato textual.
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
                        res.append((name, a["local"], a["prefixlen"]))
            return res
        except: pass
    # Fallback textual
    res=[]
    for ln in sh("ip -o -4 addr show").splitlines():
        m = re.search(r'^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)', ln)
        if not m: continue
        ifname, ip, plen = m.group(1), m.group(2), int(m.group(3))
        if ifname == "lo": continue
        res.append((ifname, ip, plen))
    return res

def p2p_peers():
    """
    Descobre automaticamente, para cada interface /30, qual é o IP do "peer" (vizinho).
    Regra do /30: os hosts válidos terminam em .1 e .2 (na mesma sub-rede).
    Se eu sou .1, meu peer é .2 (e vice-versa).
    Retorna lista de tuplas (ifname, meu_ip, ip_do_peer).
    """
    peers=[]
    for ifname, ip, plen in ip_ifaces():
        if plen != 30: continue  # só consideramos os enlaces ponto-a-ponto /30
        try:
            addr = ipaddress.ip_interface(f"{ip}/{plen}")
            last = int(str(addr.ip).split(".")[-1])
            if last not in (1,2): continue
            peer_last = 3 - last  # se sou 1 -> 2; se sou 2 -> 1
            octs = str(addr.network.network_address).split(".")
            octs[-1] = str(peer_last)
            peers.append((ifname, ip, ".".join(octs)))
        except: continue
    return peers

def now_ms(): 
    """Retorna o tempo atual em milissegundos (inteiro). Usado para medir RTT."""
    return int(time.time()*1000)

def link_cost(st):
    """
    Calcula o custo de um enlace a partir do estado do vizinho:
      custo = 1*RTT + 4*Jitter + 1*Hop
    - RTT e Jitter vêm do HELLO/SONDA (EWMA).
    - Hop é 1 (constante) para "puxar" levemente por menos saltos.
    """
    rtt = st.get("rtt_ms", 20.0)  # valor padrão razoável caso ainda não tenha medido
    jit = st.get("jit_ms", 1.0)
    return W_RTT*rtt + W_JIT*jit + W_HOP*1.0

def dijkstra(adj, src):
    """
    Implementação padrão do algoritmo de Dijkstra.
    Entrada:
      - adj: dicionário de adjacências {u: {v: custo(u->v)}}
      - src: nó de origem (ROUTER_ID)
    Saída:
      - dist: menor custo de src até cada nó
      - prev: "pai" de cada nó no caminho ótimo (para reconstruir o 1º salto)
    """
    INF=1e18; dist=defaultdict(lambda: INF); prev={}
    dist[src]=0; pq=[(0,src)]  # fila de prioridade com pares (distância, nó)
    while pq:
        d,u=heapq.heappop(pq)
        if d!=dist[u]: continue  # ignora entradas desatualizadas
        for v,c in adj[u].items():
            nd=d+c
            if nd<dist[v]-1e-9:
                dist[v]=nd; prev[v]=u
                heapq.heappush(pq,(nd,v))
    return dist, prev

def first_hop(prev, dest, me):
    """
    Dado o vetor 'prev' de Dijkstra, percorre para trás a partir de 'dest'
    até descobrir qual é o primeiro vizinho a partir de 'me' no caminho ótimo.
    Retorna o RID do primeiro salto (ou None se não houver caminho).
    """
    cur=dest; p=prev.get(cur)
    if not p: return None
    while p and p!=me:
        cur=p; p=prev.get(cur)
    return cur

# ---------- Rede (socket) ----------
# Criamos um socket UDP para enviar/receber mensagens do protocolo.
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
# Habilita broadcast (não é essencial aqui, mas não atrapalha).
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
# Bind na porta 55200 em todos os endereços (escuta pacotes de qualquer vizinho).
sock.bind(("", PORT))

def flood(msg, jitter_ms=0, exclude_ip=None):
    """
    Reenvia (inunda) uma mensagem LSA para todos os vizinhos (exceto, opcionalmente, 
    o IP de quem nos mandou – para evitar eco imediato).
    Um pequeno 'jitter' aleatório ajuda a reduzir tempestades de pacotes sincronizadas.
    """
    if jitter_ms: time.sleep(jitter_ms/1000.0)
    data = json.dumps(msg).encode()
    with state_lock:
        nb = list(neighbors.items())
    for _rid, st in nb:
        ip = st.get("ip")
        if ip and ip != exclude_ip:
            try: sock.sendto(data, (ip, PORT))
            except: pass  # se falhar envio para um vizinho, seguimos nos demais

def install_routes():
    """
    Constrói o grafo (adj) a partir da LSDB e roda Dijkstra.
    Para cada LAN de destino conhecida, descobre o 1º salto e instala a rota no Linux.
    Escreve um snapshot de `ip route` em /tmp/lyra_{RID}.routes para debug.
    """
    # 1) Montar o grafo de adjacências a partir do que temos na LSDB.
    with state_lock:
        adj = defaultdict(dict)
        for u, nbrs in lsdb.items():
            for v, c in nbrs.items():
                c = max(0.1, float(c))  # custo nunca 0 (evita arestas "gratuitas")
                # Guardamos a menor observação entre u->v e também forçamos a simetria v->u
                if c < adj[u].get(v, 1e18): adj[u][v] = c
                if c < adj[v].get(u, 1e18): adj[v][u] = c
        ip_map = dict(ip_by_rid)  # cópia local (evita lock longo)

    # 2) Rodar Dijkstra a partir de mim (ROUTER_ID).
    dist, prev = dijkstra(adj, ROUTER_ID)

    # 3) Para cada rede LAN destino, instalar rota via o 1º salto.
    changed=False
    for dst_rid, pfx in LAN_PREFIXES.items():
        if dst_rid == ROUTER_ID: continue                # não roteio para a minha própria LAN
        if dist.get(dst_rid, 1e18) >= 1e17: continue     # sem caminho conhecido, ignore
        nh = first_hop(prev, dst_rid, ROUTER_ID)         # descobre o primeiro vizinho no caminho ótimo
        if nh and ip_map.get(nh):
            # Instala/atualiza rota no kernel. `replace` cria ou substitui.
            subprocess.call(f"ip route replace {pfx} via {ip_map[nh]}", shell=True)
            changed=True
    if changed:
        # Salva uma cópia da tabela de rotas para depuração rápida.
        open(f"/tmp/lyra_{ROUTER_ID}.routes","w").write(sh("ip route"))

def send_full_lsa():
    """
    Gera e envia um LSA contendo todos os vizinhos "UP" e seus custos atuais.
    É chamado: (a) no 'bootstrap' ao iniciar o processo; (b) no primeiro HELLO de cada vizinho;
               (c) periodicamente; (d) quando um vizinho cai (para avisar a rede).
    """
    with state_lock:
        nbrs=[]
        for rid, st in neighbors.items():
            if st.get("up"):
                c = link_cost(st)
                nbrs.append([rid, c, now_ms()])  # cada item: [vizinho, custo, timestamp]
        msg={"t":"LSA","rid":ROUTER_ID,"seq":now_ms(),"ttl":LSA_TTL,"nbrs":nbrs}
    # Pequeno jitter para espalhar no tempo os floods e reduzir colisões.
    flood(msg, jitter_ms=random.randint(0,120))

# ---------- Threads ----------
def rx_loop():
    """
    Thread que RECEBE pacotes da rede (HELLO/SONDA/LSA) e atualiza o estado.
    - HELLO/SONDA: atualiza métricas do vizinho; primeiro HELLO dispara LSA e recalcula rotas.
    - LSA: atualiza LSDB, reencaminha (com TTL-1), e recalcula rotas.
    """
    while True:
        data, addr = sock.recvfrom(65535)
        try: msg = json.loads(data.decode())
        except: continue
        typ = msg.get("t"); src_ip = addr[0]
        try: rid = int(msg.get("rid", -1))
        except: continue

        if typ in ("HELLO","SONDA"):
            first_up = False  # para sabermos se é a primeira vez que esse vizinho está "UP"
            with state_lock:
                rid_by_ip[src_ip] = rid
                ip_by_rid[rid] = src_ip
                # Se não existir, cria entrada com valores iniciais.
                st = neighbors.setdefault(
                    rid, {"ip":src_ip,"rtt_ms":20.0,"jit_ms":1.0,"missed":0,"up":False}
                )
                # RTT = (agora - timestamp enviado pelo vizinho).
                rtt = max(0, now_ms() - int(msg["ts"]))
                # EWMA do RTT: mistura valor antigo com o novo, ponderado por ALPHA.
                old = st["rtt_ms"]
                st["rtt_ms"] = (1-ALPHA)*old + ALPHA*rtt
                # EWMA do Jitter: aproxima a variação entre RTT atual e anterior.
                st["jit_ms"] = (1-BETA)*st["jit_ms"] + BETA*abs(st["rtt_ms"]-old)
                st["missed"] = 0  # zeramos o "contador de HELLOs perdidos"
                if not st["up"]:
                    # Se estava "down" e agora recebemos HELLO, marca como "up"
                    st["up"] = True
                    first_up = True
            # Sempre que chegam medições, tentamos recalcular rotas (pode mudar custo).
            install_routes()
            if first_up:
                # No primeiro HELLO desse vizinho, anuncia LSA para sincronizar a rede cedo.
                send_full_lsa()

        elif typ == "LSA":
            # Evita processar/reenviar o mesmo LSA várias vezes.
            key = (rid, int(msg.get("seq",0)))
            if key in seen: 
                continue
            seen.add(key)
            # Atualiza a LSDB com os pares (vizinho, custo) informados pelo emissor do LSA.
            with state_lock:
                nbrs = {}
                for v,c,_ts in msg.get("nbrs", []):
                    nbrs[int(v)] = float(c)
                lsdb[rid] = nbrs
            # Repassa o LSA adiante se ainda tiver "TTL".
            ttl = int(msg.get("ttl",1))
            if ttl > 0:
                msg["ttl"] = ttl - 1
                flood(msg, jitter_ms=random.randint(0,120), exclude_ip=src_ip)
            # Qualquer mudança de LSDB pode alterar melhor caminho -> recalcula rotas.
            install_routes()

def send_hello():
    """
    Thread que envia HELLO periodicamente para cada peer /30 descoberto.
    Cada HELLO carrega um timestamp (ts) para o vizinho medir RTT.
    """
    while True:
        for _ifn, myip, peer in p2p_peers():
            msg={"t":"HELLO","rid":ROUTER_ID,"ts":now_ms(),"ip":myip}
            try: sock.sendto(json.dumps(msg).encode(), (peer, PORT))
            except: pass
        time.sleep(HELLO_INT)

def liveness_monitor():
    """
    Thread que verifica se vizinhos "sumiram".
    A cada HELLO_INT segundos, incrementa o contador 'missed'.
    Se 'missed' >= DEAD_INT, marca o vizinho como 'down' e anuncia LSA para toda a rede.
    """
    while True:
        time.sleep(HELLO_INT)
        fallen=False
        with state_lock:
            for rid, st in neighbors.items():
                st["missed"] = st.get("missed",0) + 1
                # Se o vizinho estava 'up' e "perdemos" DEAD_INT HELLOs, derruba.
                if st["missed"] >= DEAD_INT and st.get("up",True):
                    st["up"] = False
                    fallen=True
        if fallen:
            # Anuncia LSA para que todos saibam que certos enlaces ficaram indisponíveis.
            send_full_lsa()
            # Recalcula caminhos após a mudança de topologia.
            install_routes()

def periodic_lsa():
    """
    Thread que envia LSA full periodicamente (a cada LSA_BASE segundos).
    Serve como "relógio de batimento" para re-sincronizar a visão da rede.
    """
    while True:
        time.sleep(LSA_BASE)
        send_full_lsa()

def bootstrap_lsa():
    """
    Envia um LSA full assim que o processo inicia (após 0,5s).
    Isso acelera a convergência inicial sem esperar o período de 10s.
    """
    time.sleep(0.5)
    send_full_lsa()

def main():
    """
    Função principal: cria as threads e mantém o processo vivo.
    """
    # Thread de recepção e processamento das mensagens.
    threading.Thread(target=rx_loop,          daemon=True).start()
    # Thread que envia HELLO e mede RTT/Jitter continuamente.
    threading.Thread(target=send_hello,       daemon=True).start()
    # Thread que detecta vizinhos "down".
    threading.Thread(target=liveness_monitor, daemon=True).start()
    # Thread do LSA periódico (backup de sincronização).
    threading.Thread(target=periodic_lsa,     daemon=True).start()
    # LSA inicial (bootstrap).
    threading.Thread(target=bootstrap_lsa,    daemon=True).start()
    # Loop principal apenas mantém o processo rodando.
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()