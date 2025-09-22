#!/usr/bin/python3
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.cli import CLI
from mininet.log import setLogLevel, info

class LyraTopo(Topo):
    """Topologia customizada para o protocolo LYRA com 5 roteadores e 3 hosts."""
    def build(self):
        # Adiciona 5 nós que atuarão como roteadores
        r = {i: self.addHost(f"r{i}") for i in range(1, 6)}
        
        # Adiciona 3 nós que atuarão como hosts de LAN
        h1, h3, h5 = self.addHost("h1"), self.addHost("h3"), self.addHost("h5")

        # Adiciona os links entre os roteadores com seus respectivos delays e bandas
        self.addLink(r[1], r[2], cls=TCLink, delay='10ms', bw=100)
        self.addLink(r[1], r[3], cls=TCLink, delay='14ms', bw=100)
        self.addLink(r[2], r[3], cls=TCLink, delay='6ms',  bw=100)
        self.addLink(r[2], r[4], cls=TCLink, delay='19ms', bw=100)
        self.addLink(r[3], r[5], cls=TCLink, delay='8ms',  bw=100)
        self.addLink(r[4], r[5], cls=TCLink, delay='10ms', bw=100)
        
        # Adiciona os links entre os hosts e seus roteadores de borda
        self.addLink(h1, r[1], cls=TCLink, bw=1000)
        self.addLink(h3, r[3], cls=TCLink, bw=1000)
        self.addLink(h5, r[5], cls=TCLink, bw=1000)

def configIPs(net):
    """Configura os endereços IP em todas as interfaces da topologia."""
    info('*** Configuring router-to-router links (/30)\n')
    pairs = [
        ('r1','r2','10.12.0.'), ('r1','r3','10.13.0.'), ('r2','r3','10.23.0.'),
        ('r2','r4','10.24.0.'), ('r3','r5','10.35.0.'), ('r4','r5','10.45.0.'),
    ]
    for a, b, base in pairs:
        na, nb = net.get(a), net.get(b)
        # Encontra as interfaces corretas da conexão
        ia, ib = na.connectionsTo(nb)[0]
        na.setIP(f'{base}1/30', intf=ia)
        nb.setIP(f'{base}2/30', intf=ib)

    info('*** Configuring LAN links and default routes (/24)\n')
    # Configura IPs e rotas para h1
    r1, h1 = net.get('r1'), net.get('h1')
    r1.setIP('10.1.0.1/24', intf=r1.connectionsTo(h1)[0][0])
    h1.setIP('10.1.0.10/24', intf=h1.connectionsTo(r1)[0][0])
    h1.cmd('ip route add default via 10.1.0.1')
    
    # Configura IPs e rotas para h3
    r3, h3 = net.get('r3'), net.get('h3')
    r3.setIP('10.3.0.1/24', intf=r3.connectionsTo(h3)[0][0])
    h3.setIP('10.3.0.10/24', intf=h3.connectionsTo(r3)[0][0])
    h3.cmd('ip route add default via 10.3.0.1')

    # Configura IPs e rotas para h5
    r5, h5 = net.get('r5'), net.get('h5')
    r5.setIP('10.5.0.1/24', intf=r5.connectionsTo(h5)[0][0])
    h5.setIP('10.5.0.10/24', intf=h5.connectionsTo(r5)[0][0])
    h5.cmd('ip route add default via 10.5.0.1')

    info('*** Enabling IP forwarding on all routers\n')
    for i in range(1, 6):
        net.get(f'r{i}').cmd('sysctl -w net.ipv4.ip_forward=1')

if __name__ == '__main__':
    setLogLevel('info')
    
    info('*** Creating the network\n')
    net = Mininet(topo=LyraTopo(), link=TCLink, controller=None, switch=OVSBridge)
    
    info('*** Starting the network\n')
    net.start()
    
    # Configura todos os IPs e rotas estáticas
    configIPs(net)

    info('*** Starting routing protocol on routers\n')
    for i in range(1, 6):
        router = net.get(f'r{i}')
        # Executa o script em background, passando o RID correto
        router.cmd(f'RID={i} python3 lyra.py &')
        info(f'   -> Started LYRA on r{i}\n')
    
    # Inicia a linha de comando interativa para testes
    CLI(net)
    
    info('*** Stopping the network\n')
    net.stop()
