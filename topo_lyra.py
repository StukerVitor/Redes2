from mininet.topo import Topo
from mininet.net import Mininet
from mininet.link import TCLink
from mininet.node import OVSBridge
from mininet.cli import CLI
from mininet.log import setLogLevel

class LyraTopo(Topo):
    def build(self):
        r = {i: self.addHost(f"r{i}") for i in range(1,6)}
        h1, h3, h5 = self.addHost("h1"), self.addHost("h3"), self.addHost("h5")

        self.addLink(r[1], r[2], cls=TCLink, delay='10ms', bw=100)
        self.addLink(r[1], r[3], cls=TCLink, delay='14ms', bw=100)
        self.addLink(r[2], r[3], cls=TCLink, delay='6ms',  bw=100)
        self.addLink(r[2], r[4], cls=TCLink, delay='19ms', bw=100)
        self.addLink(r[3], r[5], cls=TCLink, delay='8ms',  bw=100)
        self.addLink(r[4], r[5], cls=TCLink, delay='10ms', bw=100)

        self.addLink(h1, r[1], cls=TCLink, bw=1000)
        self.addLink(h3, r[3], cls=TCLink, bw=1000)
        self.addLink(h5, r[5], cls=TCLink, bw=1000)

def configIPs(net):
    pairs = [
        ('r1','r2','10.12.0.'),
        ('r1','r3','10.13.0.'),
        ('r2','r3','10.23.0.'),
        ('r2','r4','10.24.0.'),
        ('r3','r5','10.35.0.'),
        ('r4','r5','10.45.0.'),
    ]
    for a,b,base in pairs:
        na, nb = net.get(a), net.get(b)
        ia = na.connectionsTo(nb)[0][0]
        ib = na.connectionsTo(nb)[0][1]
        na.setIP(base+'1/30', intf=ia)
        nb.setIP(base+'2/30', intf=ib)

    r1, r3, r5 = net['r1'], net['r3'], net['r5']
    h1, h3, h5 = net['h1'], net['h3'], net['h5']

    r1.setIP('10.1.0.1/24', intf=r1.connectionsTo(h1)[0][0]); h1.setIP('10.1.0.10/24', intf=h1.connectionsTo(r1)[0][0])
    r3.setIP('10.3.0.1/24', intf=r3.connectionsTo(h3)[0][0]); h3.setIP('10.3.0.10/24', intf=h3.connectionsTo(r3)[0][0])
    r5.setIP('10.5.0.1/24', intf=r5.connectionsTo(h5)[0][0]); h5.setIP('10.5.0.10/24', intf=h5.connectionsTo(r5)[0][0])

    for h, gw in [(h1,'10.1.0.1'), (h3,'10.3.0.1'), (h5,'10.5.0.1')]:
        h.cmd(f'ip route add default via {gw}')
    for i in range(1,6):
        net[f'r{i}'].cmd('sysctl -w net.ipv4.ip_forward=1')

if __name__ == '__main__':
    setLogLevel('info')
    net = Mininet(topo=LyraTopo(), link=TCLink, controller=None, switch=OVSBridge)
    net.start(); configIPs(net); CLI(net); net.stop()