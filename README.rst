========================
Team and repository tags
========================

.. image:: http://governance.openstack.org/badges/networking-ofagent.svg
    :target: http://governance.openstack.org/reference/tags/index.html

.. Change things from this point on

What's this
===========

This is OpenStack/Networking (Neutron) "ofagent" ML2 driver and its agent.

Installation
============

For how to install/set up ML2 mechanism driver for OpenFlow Agent, please refer to
https://github.com/osrg/ryu/wiki/OpenStack

Notes for updating from Icehouce
================================

OVS.bridge_mappings is deprecated for ofagent.  It was removed in Kilo.
Please use AGENT.physical_interface_mappings instead.
To mimic an existing setup with bridge_mapping, you can create
a veth pair, link one side of it to the bridge, and then specify
the other side in physical_interface_mappings.
For example, if you have the following::

    [OVS]
    bridge_mappings=public:br-ex

You can do::

    # ip link add int-public type veth peer name phy-public
    # ip link set int-public up
    # ip link set phy-public up
    # ovs-vsctl add-port br-ex phy-public

and then replace the bridge_mappings with::

    [AGENT]
    physical_interface_mappings=public:int-public

After Icehouce, most of the functionality have been folded into
a single bridge, the integration bridge.  (aka. br-int)
The integration bridge is the only bridge which would have an
OpenFlow connection to the embedded controller in ofagent now.

- ofagent no longer uses a separate bridge for tunneling.
  Please remove br-tun if you have one::

   # ovs-vsctl del-br br-tun

- ofagent no longer acts as an OpenFlow controller for physical bridges.
  Please remove set-controller configuration from your physical bridges::

   # ovs-vsctl del-controller ${PHYSICAL_BRIDGE}

The support of ancillary bridges has been removed after Icehouce.
While you can still use these bridges to provide connectivity,
neutron-ofagent-agent no longer reports port state changes (up/down)
for these bridges.  If it is a problem for you, please consider
tweaking your configuration to avoid using ancillary bridges.
We recommend to use a provider network instead as the following:

- Make l3-agent external_network_bridge configuration empty::

    [DEFAULT]
    external_network_bridge=

- (Re-)create a network (and subnet) for public connectivity with
  a flat provider network::

    neutron net-create $PUBLIC_NETWORK -- \
      --router:external=True \
      --provider:network_type:flat \
      --provider:physical_network=$PUBLIC_PHYSICAL_NETWORK

- Associate your neutron router to the above network::

    neutron router-gateway-clear $ROUTER_ID
    neutron router-gateway-set $ROUTER_ID $PUBLIC_NETWORK

- Add the corresponding entry to bridge_mappings::

    [OVS]
    bridge_mappings=$PUBLIC_PHYSICAL_NETWORK:$PUBLIC_BRIDGE

The port naming scheme for ofagent has been changed after Icehouce.
If you are using security groups, you should switch firewall_driver
accordingly.

  From::

    [securitygroup]
    firewall_driver=neutron.agent.linux.iptables_firewall.OVSHybridIptablesFirewallDriver

  To::

    [securitygroup]
    firewall_driver=neutron.agent.linux.iptables_firewall.IptablesFirewallDriver

External Resources
==================

OFAgent documentation on ReadTheDocs
------------------------------------

http://networking-ofagent.readthedocs.org/en/latest/

Neutron/OFAgent on OpenStack wiki
---------------------------------

https://wiki.openstack.org/wiki/Neutron/OFAgent

Ryu
---

OFAgent uses Ryu ofproto library to communicate with the local switch.

For general Ryu stuff, please refer to
http://osrg.github.io/ryu/

Ryu is available at github
git://github.com/osrg/ryu.git
https://github.com/osrg/ryu

The mailing is at
ryu-devel@lists.sourceforge.net
https://lists.sourceforge.net/lists/listinfo/ryu-devel

Enjoy!
