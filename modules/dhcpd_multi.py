# Copyright 2013 James McCauley
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Based on dhcpd.py.

2017 Adam Calabrigo
CH-CH-CHANGES:

Makes the leases actually time out. Adds support for multiple subnets on the
network.
"""

from pox.core import core
import pox.openflow.libopenflow_01 as of
import pox.lib.packet as pkt
from pox.lib.packet.arp import arp
from pox.lib.packet.ethernet import ethernet

from pox.lib.addresses import IPAddr,EthAddr,parse_cidr
from pox.lib.addresses import IP_BROADCAST, IP_ANY
from pox.lib.revent import *
from pox.lib.util import dpid_to_str
from pox.lib.recoco import Timer

import time
import yaml
import os

GATEWAY_DUMMY_MAC = '03:00:00:00:be:ef'

log = core.getLogger()

# Times (in seconds) to use for differente timouts:
timeoutSec = dict(
  timerInterval=5,      # Seconds between timer routine activations
  leaseInterval=60*60  # Time until DHCP leases expire - 1 hour
  )

def ip_for_event (event):
  """
  Use a switch's DPID as an EthAddr
  """
  eth = dpid_to_str(event.dpid,True).split("|")[0].replace("-",":")
  return EthAddr(eth)

def dpid_to_mac (dpid):
    return EthAddr("%012x" % (dpid & 0xffFFffFFffFF,))

class Alive (object): # from host_tracker
  """
  Holds liveliness information for address pool entries
  """
  def __init__ (self, livelinessInterval=timeoutSec['leaseInterval']):
    self.lastTimeSeen = time.time()
    self.interval=livelinessInterval

  def expired (self):
    return time.time() > self.lastTimeSeen + self.interval

  def refresh (self):
    self.lastTimeSeen = time.time()

class LeaseEntry (Alive):
  """
  Leased IP address.
  """
  def __init__ (self, ip):
    super(LeaseEntry,self).__init__()
    self.ip = IPAddr(ip)

  def __str__(self):
    return str(self.ip)

  def __eq__ (self, other):
    if other is None:
      return False
    elif type(other) == IPAddr:
      return self.ip == other

    if self.ip != other.ip:
      return False

    return True

  def __ne__ (self, other):
    return not self.__eq__(other)

class DHCPLease (Event):
  """
  Raised when a lease is given

  Call nak() to abort this lease
  """
  def __init__ (self, host_mac, ip_entry, port=None,
                dpid=None, renew=False, expire=False):
    super(DHCPLease, self).__init__()
    self.host_mac = host_mac
    self.ip = ip_entry.ip
    self.port = port
    self.dpid = dpid
    self.renew = renew
    self.expire = expire
    self._nak = False

    assert sum(1 for x in [renew, expire] if x) == 1

  def nak (self):
    self._nak = True

class AddressPool (object):
  """
  Superclass for DHCP address pools

  Note that it's just a subset of a list (thus, you can always just use
  a list as a pool).  The one exception is an optional "subnet_mask" hint.

  It probably makes sense to change this abstraction so that we can more
  easily return addresses from multiple ranges, and because some things
  (e.g., getitem) are potentially difficult to implement and not particularly
  useful (since we only need to remove a single item at a time).
  """
  def __init__ (self):
    """
    Initialize this pool.
    """
    pass

  def __contains__ (self, item):
    """
    Is this IPAddr in the pool?
    """
    return False

  def append (self, item):
    """
    Add this IP address back into the pool
    """
    pass

  def remove (self, item):
    """
    Remove this IPAddr from the pool
    """
    pass

  def __len__ (self):
    """
    Returns number of IP addresses in the pool
    """
    return 0

  def __getitem__ (self, index):
    """
    Get an IPAddr from the pool.

    Note that this will only be called with index = 0!
    """
    pass

class SimpleAddressPool (AddressPool):
  """
  Simple AddressPool for simple subnet based pools.
  """
  def __init__ (self, network = "192.168.0.0/24", first = 1, last = None,
                count = None):
    """
    Simple subnet-based address pool

    Allocates count IP addresses out of network/network_size, starting
    with the first'th.  You may specify the end of the range with either
    last (to specify the last'th address to use) or count to specify the
    number to use.  If both are None, use up to the end of all
    legal addresses.

    Example for all of 192.168.x.x/16:
      SimpleAddressPool("192.168.0.0/16", 1, 65534)
    """
    network,network_size = parse_cidr(network)

    self.first = first
    self.network_size = network_size
    self.host_size = 32-network_size
    self.network = IPAddr(network)

    # use entire host space
    if last is None and count is None:
      self.last = (1 << self.host_size) - 2
    # set last address to use
    elif last is not None:
      self.last = last
    # just use count many
    elif count is not None:
      self.last = self.first + count - 1
    else:
      raise RuntimeError("Cannot specify both last and count")

    self.removed = set()

    # error checking here
    if self.count <= 0: raise RuntimeError("Bad first/last range")
    if first == 0: raise RuntimeError("Can't allocate 0th address")
    if self.host_size < 0 or self.host_size > 32:
      raise RuntimeError("Bad network")
    if IPAddr(self.last | self.network.toUnsigned()) not in self:
      raise RuntimeError("Bad first/last range")

  def __repr__ (self):
    return str(self)

  def __str__ (self):
    t = self.network.toUnsigned()
    t = (IPAddr(t|self.first),IPAddr(t|self.last))
    return "<Addresses from %s to %s>" % t

  @property
  def subnet_mask (self):
    return IPAddr(((1<<self.network_size)-1) << self.host_size)

  @property
  def count (self):
    return self.last - self.first + 1

  def __contains__ (self, item):
    item = IPAddr(item)
    if item in self.removed: return False
    n = item.toUnsigned()
    mask = (1<<self.host_size)-1
    nm = (n & mask) | self.network.toUnsigned()
    if nm != n: return False
    if (n & mask) == mask: return False
    if (n & mask) < self.first: return False
    if (n & mask) > self.last: return False
    return True

  def append (self, item):
    item = IPAddr(item)
    if item not in self.removed:
      if item in self:
        raise RuntimeError("%s is already in this pool" % (item,))
      else:
        raise RuntimeError("%s does not belong in this pool" % (item,))
    self.removed.remove(item)

  def remove (self, item):
    item = IPAddr(item)
    if item not in self:
      raise RuntimeError("%s not in this pool" % (item,))
    self.removed.add(item)

  def __len__ (self):
    return (self.last-self.first+1) - len(self.removed)

  def __getitem__ (self, index):
    if index < 0:
      raise RuntimeError("Negative indices not allowed")
    if index >= len(self):
      raise IndexError("Item does not exist")
    c = self.first

    # Use a heuristic to find the first element faster (we hope)
    # Note this means that removing items changes the order of
    # our "list".
    c += len(self.removed)
    while c > self.last:
      c -= self.count

    while True:
      addr = IPAddr(c | self.network.toUnsigned())
      if addr not in self.removed:
        assert addr in self
        index -= 1
        if index < 0: return addr
      c += 1
      if c > self.last: c -= self.count

class DHCPServer (object):

  def __init__ (self, server, ip_address = "192.168.0.254", router_address = (),
                dns_address = (), pool = None, switches = None, subnet = None,
                install_flow = True):

    def fix_addr (addr, backup):
      if addr is None: return None
      if addr is (): return IPAddr(backup)
      return IPAddr(addr)

    self._install_flow = install_flow

    self.ip_addr = IPAddr(ip_address)
    self.router_addr = fix_addr(router_address, ip_address)
    self.dns_addr = fix_addr(dns_address, self.router_addr)
    self.server = server

    if pool is None:
      self.pool = [IPAddr("192.168.0."+str(x)) for x in range(100,199)]
      self.subnet = IPAddr(subnet or "255.255.255.0")
    else:
      self.pool = pool
      self.subnet = subnet
      if hasattr(pool, 'subnet_mask'):
        self.subnet = pool.subnet_mask
      if self.subnet is None:
        raise RuntimeError("You must specify a subnet mask or use a "
                           "pool with a subnet hint")

    self.switches = switches
    self._install_flows()
    self.lease_time = timeoutSec['leaseInterval']

    self.offers = {} # Eth -> IP we offered
    self.leases = {} # Eth -> IP we leased, as LeaseEntry

    if self.ip_addr in self.pool:
      log.debug("Removing my own IP (%s) from address pool", self.ip_addr)
      self.pool.remove(self.ip_addr)

    self._t = Timer(timeoutSec['timerInterval'], self._check_leases,
                    recurring=True)

  # on switch connect, install flow
  def _install_flows (self):
    if self._install_flow:
      msg = of.ofp_flow_mod()
      msg.match = of.ofp_match()
      msg.match.dl_type = pkt.ethernet.IP_TYPE
      msg.match.nw_proto = pkt.ipv4.UDP_PROTOCOL
      #msg.match.nw_dst = IP_BROADCAST
      msg.match.tp_src = pkt.dhcp.CLIENT_PORT
      msg.match.tp_dst = pkt.dhcp.SERVER_PORT
      msg.actions.append(of.ofp_action_output(port = of.OFPP_CONTROLLER))
      #msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))

      # add flows to the switches covered by this server
      # only listen to switches on this server
      for s in self.switches:
          self.server.dynamic_topology.switches[dpid_to_str(s)].connection.send(msg)
          self.server.dynamic_topology.switches[dpid_to_str(s)].connection.addListeners(self)

  def _get_pool (self, event):
    """
    Get an IP pool for this event.

    Return None to not issue an IP.  You should probably log this.
    """
    return self.pool

  # checks the packet, processes DHCP msg from client
  def _handle_PacketIn (self, event):
    # Is it to us?  (Or at least not specifically NOT to us...)

    ipp = event.parsed.find('ipv4')
    if not ipp or not ipp.parsed:
      return

    if ipp.dstip not in (IP_ANY,IP_BROADCAST,self.ip_addr):
      return
    nwp = ipp.payload
    if not nwp or not nwp.parsed or not isinstance(nwp, pkt.udp):
      return
    if nwp.srcport != pkt.dhcp.CLIENT_PORT:
      return
    if nwp.dstport != pkt.dhcp.SERVER_PORT:
      return
    p = nwp.payload
    if not p:
      log.debug("%s: no packet", str(event.connection))
      return
    if not isinstance(p, pkt.dhcp):
      log.debug("%s: packet is not DHCP", str(event.connection))
      return
    if not p.parsed:
      log.debug("%s: DHCP packet not parsed", str(event.connection))
      return

    if p.op != p.BOOTREQUEST:
      return

    t = p.options.get(p.MSG_TYPE_OPT)
    if t is None:
      return

    pool = self._get_pool(event)
    if pool is None:
      return

    if t.type == p.DISCOVER_MSG:
      self.exec_discover(event, p, pool)
    elif t.type == p.REQUEST_MSG:
      self.exec_request(event, p, pool)
    elif t.type == p.RELEASE_MSG:
      self.exec_release(event, p, pool)

  def _check_leases (self):
    """
    Checks for expired leases
    """

    for client in self.leases.keys():
      lease = self.leases[client]
      if lease.expired():
        log.info("Entry %s: IP address %s expired",
                 str(client), str(lease.ip) )
        self.pool.append(lease.ip)
        ev = DHCPLease(client, lease.ip, expire=True)
        self.server.raiseEvent(ev)
        del self.leases[client]
        if ev._nak:
          self.nak(event)
          return

        # if this host was mobile, delete it from mobile host table
        if client in self.server.mobile_hosts:
          del self.server.mobile_hosts[client]

  def reply (self, event, msg):
    # this seems to encapsulate our DHCP packets in the proper headers

    # fill out the rest of the DHCP packet
    orig = event.parsed.find('dhcp')
    broadcast = (orig.flags & orig.BROADCAST_FLAG) != 0
    msg.op = msg.BOOTREPLY
    msg.chaddr = event.parsed.src
    msg.htype = 1
    msg.hlen = 6
    msg.xid = orig.xid
    msg.add_option(pkt.DHCP.DHCPServerIdentifierOption(self.ip_addr))

    # create ethernet header
    ethp = pkt.ethernet(src=ip_for_event(event),dst=event.parsed.src)
    ethp.type = pkt.ethernet.IP_TYPE
    ipp = pkt.ipv4(srcip = self.ip_addr)
    ipp.dstip = event.parsed.find('ipv4').srcip
    if broadcast:
      ipp.dstip = IP_BROADCAST
      ethp.dst = pkt.ETHERNET.ETHER_BROADCAST

    # create UDP header
    ipp.protocol = ipp.UDP_PROTOCOL
    udpp = pkt.udp()
    udpp.srcport = pkt.dhcp.SERVER_PORT
    udpp.dstport = pkt.dhcp.CLIENT_PORT

    # encapsulate and reply to host
    udpp.payload = msg
    ipp.payload = udpp
    ethp.payload = ipp
    po = of.ofp_packet_out(data=ethp.pack())
    po.actions.append(of.ofp_action_output(port=event.port))
    event.connection.send(po)

  def nak (self, event, msg = None):
    if msg is None:
      msg = pkt.dhcp()
    msg.add_option(pkt.DHCP.DHCPMsgTypeOption(msg.NAK_MSG))
    msg.siaddr = self.ip_addr
    self.reply(event, msg)

  def exec_release (self, event, p, pool):
    src = event.parsed.src
    if src != p.chaddr:
      log.warn("%s tried to release %s with bad chaddr" % (src,p.ciaddr))
      return
    if self.leases.get(p.chaddr) != p.ciaddr:
      log.warn("%s tried to release unleased %s" % (src,p.ciaddr))
      return
    del self.leases[p.chaddr]
    pool.append(p.ciaddr)

    if src in self.server.mobile_hosts:
      del self.server.mobile_hosts[src]

    log.info("%s released %s" % (src,p.ciaddr))

  def exec_request (self, event, p, pool):
    # create and send ACKNOWLEDGE in response to REQUEST

    if not p.REQUEST_IP_OPT in p.options:
      # Uhhh...
      return

    # if client asks for specific IP
    wanted_ip = p.options[p.REQUEST_IP_OPT].addr
    src = event.parsed.src  # src MAC
    dpid = event.connection.dpid
    port = event.port
    got_ip = None
    is_mobile = (src in self.server.mobile_hosts)

    # renew
    if src in self.leases:
      if wanted_ip != self.leases[src]:
        # if the host is mobile but is not asking for a different address,
        # we want to consider it no longer mobile
        if is_mobile:
          del self.server.mobile_hosts[src]
        pool.append(self.leases[src].ip)
        del self.leases[src]
      else:
        got_ip = self.leases[src]
        got_ip.refresh() # this is a lease renew

    # respond to offer
    if got_ip is None:
      if src in self.offers:    # if there was an offer to this client
        if wanted_ip != self.offers[src]:
          pool.append(self.offers[src])
        else:
          got_ip = LeaseEntry(self.offers[src])
        del self.offers[src]

    # new host request
    if got_ip is None:
      if wanted_ip in pool:
        pool.remove(wanted_ip)
        got_ip = LeaseEntry(wanted_ip)
        # this host is mobile, yet it is new on our server and is asking for
        # an address on this server... we should no longer consider it mobile
        # NOTE: we let the lease expire on its current server for convenience
        if is_mobile:
          del self.server.mobile_hosts[src]

    # check for mobile host
    if got_ip is None:
     mobile_ip = self.server.mobile_hosts.get(src)
     # mobile host already discovered, renew lease from original server
     if mobile_ip is not None and mobile_ip == wanted_ip:
       log.info('{0} recognized mobile host {1}'.format(self.ip_addr, src))
       subnet = [s for s in self.server.subnets.itervalues() if src in s.leases]
       if subnet is None:
         raise RuntimeError("%s designated mobile but not found on server" % (src,))
         return
       if len(subnet) > 1:
         raise RuntimeError("%s found on multiple servers" % (src,))
         return
       subnet = subnet[0]
       subnet.exec_request(event, p, subnet.pool)
       return

     else: # new mobile host found
       for subnet in [s for s in self.server.subnets.itervalues() if s != self]:
         mobile_ip = subnet.leases.get(src)
         if mobile_ip is not None and mobile_ip.ip == wanted_ip:
           log.info("%s is now mobile with IP %s", src, wanted_ip)
           self.server.mobile_hosts[src] = wanted_ip
           subnet.exec_request(event, p, subnet.pool)
           return

    if got_ip is None:
      log.warn("%s asked for un-offered %s", src, wanted_ip)
      self.nak(event)
      return

    assert got_ip == wanted_ip
    self.leases[src] = got_ip
    ev = DHCPLease(src, got_ip, port, dpid, renew=True)
    self.server.raiseEvent(ev)
    if ev._nak:
      self.nak(event)
      return
    log.info("%s leased %s to %s" % (self.ip_addr, got_ip, src))

    # create ack reply
    reply = pkt.dhcp()
    reply.add_option(pkt.DHCP.DHCPMsgTypeOption(p.ACK_MSG))
    reply.yiaddr = wanted_ip
    reply.siaddr = self.ip_addr

    wanted_opts = set()
    if p.PARAM_REQ_OPT in p.options:
      wanted_opts.update(p.options[p.PARAM_REQ_OPT].options)
    self.fill(wanted_opts, reply)

    self.reply(event, reply)

  def exec_discover (self, event, p, pool):
    # creates an OFFER in response to a DISCOVER
    reply = pkt.dhcp()
    reply.add_option(pkt.DHCP.DHCPMsgTypeOption(p.OFFER_MSG))
    src = event.parsed.src

    # if this host already has a lease
    if src in self.leases:
      offer = self.leases[src].ip   # offer it the same address
      del self.leases[src]
      self.offers[src] = offer      # move from leases to offers


    # otherwise check if we already offered an address to this host
    else:
      offer = self.offers.get(src)
      if offer is None:
        if len(pool) == 0:
          log.error("Out of IP addresses")
          self.nak(event)
          return

        offer = pool[0] # offer the first available address
        if p.REQUEST_IP_OPT in p.options: # if host requested specific address
          wanted_ip = p.options[p.REQUEST_IP_OPT].addr
          if wanted_ip in pool:
            offer = wanted_ip
        pool.remove(offer)
        self.offers[src] = offer
    reply.yiaddr = offer            # your IP
    reply.siaddr = self.ip_addr     # server's IP

    wanted_opts = set()
    if p.PARAM_REQ_OPT in p.options:
      wanted_opts.update(p.options[p.PARAM_REQ_OPT].options)
    self.fill(wanted_opts, reply)

    self.reply(event, reply)

  def fill (self, wanted_opts, msg):
    """
    Fill out some options in msg
    """
    if msg.SUBNET_MASK_OPT in wanted_opts:
      msg.add_option(pkt.DHCP.DHCPSubnetMaskOption(self.subnet))
    if msg.ROUTERS_OPT in wanted_opts and self.router_addr is not None:
      msg.add_option(pkt.DHCP.DHCPRoutersOption(self.router_addr))
    if msg.DNS_SERVER_OPT in wanted_opts and self.dns_addr is not None:
      msg.add_option(pkt.DHCP.DHCPDNSServersOption(self.dns_addr))
    msg.add_option(pkt.DHCP.DHCPIPAddressLeaseTimeOption(self.lease_time))


class DHCPD (EventMixin):
  '''
  DHCP Server that handles multiple subnets in the network.
  '''
  _eventMixin_events = set([DHCPLease])

  def __init__(self, conf):
      self.conf = conf
      self.subnets = {}  # num -> DHCPServer
      self.mobile_hosts = {} # MAC -> IP
      self.routers = [] # gateway IPs
      self.central_switches = []

      core.listen_to_dependencies(self, ['dynamic_topology'], short_attrs=True)

  def _handle_dynamic_topology_StableEvent(self, event):
    #TODO: allow dynamic updates to the YAML file if new switches
    #      are added dynamically
    if event.stable:
      try:
        with open(self.conf, 'r') as f:
          config = yaml.load(f)
          if config is None:
            log.info("Couldn't load server configuration")
            return

          log.info('Loading DHCP server configuration from {0}...'.format(self.conf))

          seen_switches = []

          # look through subnets
          for subnet in config:
            if subnet[:6] == 'subnet':  # ignore random entries
              num = int(subnet[6:])
              if 'switches' not in config[subnet]: return
              if 'net' not in config[subnet]: return
              if 'router' not in config[subnet]: return
              if 'dns' not in config[subnet]: return
              if 'range' not in config[subnet]: return
              if num in self.subnets:
                log.debug("Ignoring duplicate subnet{0}".format(num))
                return

              ran = config[subnet].get('range')
              first = 1
              last = None
              if ran is not None:
                if len(ran) is 0:
                  pass
                elif len(ran) is 1:
                  first = int(config[subnet]['range'][0])
                elif len(ran) is 2:
                  last = int(config[subnet]['range'][1])

              switches = config[subnet]['switches']
              seen_switches += [dpid_to_str(s) for s in switches]

              pool = SimpleAddressPool(network = config[subnet]['net'],
                                       first = first,
                                       last = last)
              self.subnets[num] = DHCPServer(server=self,
                                             ip_address=config[subnet]['router'],
                                             router_address=(),
                                             dns_address=config[subnet]['dns'],
                                             pool=pool,
                                             switches=switches)
              self.routers.append(IPAddr(config[subnet]['router']))
              log.info('{0} serves subnet {1} on switches {2}'.format(config[subnet]['router'],
                                                                      config[subnet]['net'],
                                                                      switches))
              self.central_switches = [s for s in self.dynamic_topology.switches
                                       if s not in seen_switches]
      except:
        log.info('Error loading {0}'.format(self.conf))

  def get_subnet(self, ip_addr):
    '''
    Given an IP, return the network address/subnet_mask.
    '''

    subnet = [self.subnets[s] for s in self.subnets if ip_addr in self.subnets[s].pool.removed]
    if len(subnet) != 1:
      raise RuntimeError("{0} is not on a valid subnet in this network".format(ip_addr))
      return None
    subnet = subnet[0]
    network, subnet_mask = subnet.pool.network, subnet.pool.subnet_mask
    return str(network) + '/' + str(subnet_mask)

  def is_router(self, ip_addr):
    '''
    Is this IP one of our router interfaces?
    '''
    return ip_addr in self.routers

  def is_central(self, dpid):
    '''
    Does this DPID identify a central switch in our network?
    '''
    return (str(dpid) in self.central_switches)

  def is_local_path(self, path):
    '''
    Is this traffic localized to one subnet?
    '''
    central_switches = [node for node in path[1:-1] if node in self.central_switches]
    return central_switches == []


def launch (conf='dhcpd_conf.yaml'):
  log.info('Config: {0}'.format(conf))
  core.register('dhcpd_multi', DHCPD(conf))