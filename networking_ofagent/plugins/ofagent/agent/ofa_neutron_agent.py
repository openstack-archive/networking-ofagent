# Copyright (C) 2014,2015 VA Linux Systems Japan K.K.
# Copyright (C) 2014,2015 YAMAMOTO Takashi <yamamoto at valinux co jp>
# Copyright (C) 2014,2015 Fumihiko Kakuma <kakuma at valinux co jp>
# All Rights Reserved.
#
# Based on openvswitch agent.
#
# Copyright 2011 VMware, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time

from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_log import log as logging
import oslo_messaging
from oslo_service import loopingcall
from ryu.app.ofctl import api as ryu_api
from ryu.base import app_manager
import ryu.cfg as ryu_cfg
from ryu.controller import handler
from ryu.controller import ofp_event
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3 as ryu_ofp13

from neutron.agent.common import ovs_lib
from neutron.agent.linux import ip_lib
from neutron.agent import rpc as agent_rpc
from neutron.agent import securitygroups_rpc as sg_rpc
from neutron.common import constants as n_const
from neutron.common import topics
from neutron.common import utils as n_utils
from neutron import context
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2.drivers.l2pop.rpc_manager import l2population_rpc
from neutron.plugins.ml2.drivers.openvswitch.agent.common import constants

from networking_ofagent.i18n import _LE, _LI, _LW
from networking_ofagent.plugins.ofagent.agent import arp_lib
from networking_ofagent.plugins.ofagent.agent import constants as ofa_const
from networking_ofagent.plugins.ofagent.agent import flows
from networking_ofagent.plugins.ofagent.agent import monitor
from networking_ofagent.plugins.ofagent.agent import ports
from networking_ofagent.plugins.ofagent.agent import tables


LOG = logging.getLogger(__name__)
cfg.CONF.import_group('AGENT',
                      'networking_ofagent.plugins.ofagent.common.config')


# A class to represent a VIF (i.e., a port that has 'iface-id' and 'vif-mac'
# attributes set).
class LocalVLANMapping(object):
    def __init__(self, vlan, network_type, physical_network, segmentation_id,
                 vif_ports=None):
        assert(isinstance(vlan, (int, long)))
        if vif_ports is None:
            vif_ports = {}
        self.vlan = vlan
        self.network_type = network_type
        self.physical_network = physical_network
        self.segmentation_id = segmentation_id
        self.vif_ports = vif_ports
        # set of remote_ips on which packets should be flooded
        self.tun_remote_ips = set()

    def __str__(self):
        return ("lv-id = %s type = %s phys-net = %s phys-id = %s" %
                (self.vlan, self.network_type, self.physical_network,
                 self.segmentation_id))


class Bridge(flows.OFAgentIntegrationBridge, ovs_lib.OVSBridge):
    def __init__(self, br_name, ryuapp):
        super(Bridge, self).__init__(br_name)
        self.datapath_id = None
        self.datapath = None
        self.ryuapp = ryuapp
        self.set_app(ryuapp)

    def find_datapath_id(self):
        self.datapath_id = self.get_datapath_id()

    def get_datapath(self, retry_max=cfg.CONF.AGENT.get_datapath_retry_times):
        retry = 0
        while self.datapath is None:
            self.datapath = ryu_api.get_datapath(self.ryuapp,
                                                 int(self.datapath_id, 16))
            retry += 1
            if retry >= retry_max:
                LOG.error(_LE('Agent terminated!: Failed to get a datapath.'))
                raise SystemExit(1)
            time.sleep(1)
        self.set_dp(self.datapath)

    def setup_ofp(self, controller_names=None,
                  protocols='OpenFlow13',
                  retry_max=cfg.CONF.AGENT.get_datapath_retry_times):
        if not controller_names:
            host = ryu_cfg.CONF.ofp_listen_host
            if not host:
                # 127.0.0.1 is a default for agent style of controller
                host = '127.0.0.1'
            controller_names = ["tcp:%s:%d" %
                                (host, ryu_cfg.CONF.ofp_tcp_listen_port)]
        try:
            self.set_protocols(protocols)
            self.set_controller(controller_names)
        except RuntimeError:
            LOG.exception(_LE("Agent terminated"))
            raise SystemExit(1)
        self.find_datapath_id()
        self.get_datapath(retry_max)


# RyuApp derives from `object`, but pylint can't see that without
# having the ryu libraries available.
# pylint: disable=super-on-old-class
class OFANeutronAgentRyuApp(app_manager.RyuApp):
    OFP_VERSIONS = [ryu_ofp13.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(OFANeutronAgentRyuApp, self).__init__(*args, **kwargs)
        self.arplib = arp_lib.ArpLib(self)
        self.monitor = monitor.PortMonitor()

    def start(self):
        super(OFANeutronAgentRyuApp, self).start()
        return hub.spawn(self._agent_main, self)

    def _agent_main(self, ryuapp):
        cfg.CONF.register_opts(ip_lib.OPTS)
        n_utils.log_opt_values(LOG)

        try:
            agent_config = create_agent_config_map(cfg.CONF)
        except ValueError:
            LOG.exception(_LE("Agent failed to create agent config map"))
            raise SystemExit(1)

        agent = OFANeutronAgent(ryuapp, **agent_config)
        self.arplib.set_bridge(agent.int_br)

        # Start everything.
        LOG.info(_LI("Agent initialized successfully, now running... "))
        agent.daemon_loop()

    @handler.set_ev_cls(ofp_event.EventOFPPacketIn, handler.MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        self.arplib.packet_in_handler(ev)

    def add_arp_table_entry(self, network, ip, mac):
        self.arplib.add_arp_table_entry(network, ip, mac)

    def del_arp_table_entry(self, network, ip):
        self.arplib.del_arp_table_entry(network, ip)

    @handler.set_ev_cls(ofp_event.EventOFPPortStatus, handler.MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        self.monitor.port_status_handler(ev)

    def get_port_status_list(self):
        return self.monitor.get_port_status_list()


class OFANeutronAgent(sg_rpc.SecurityGroupAgentRpcCallbackMixin,
                      l2population_rpc.L2populationRpcCallBackTunnelMixin):
    """A agent for OpenFlow Agent ML2 mechanism driver.

    OFANeutronAgent is a OpenFlow Agent agent for a ML2 plugin.
    This is as a ryu application thread.
    This has the following features.
    - An agent acts as an OpenFlow controller on each compute nodes.
    - OpenFlow 1.3 (vendor agnostic unlike OVS extensions).
    - l2-population is mandatory.
    """

    # history
    #   1.0 Initial version
    #   1.1 Support Security Group RPC
    #   1.3 Added param devices_to_update to security_groups_provider_updated
    target = oslo_messaging.Target(version='1.3')

    def __init__(self, ryuapp, integ_br, local_ip,
                 bridge_mappings, interface_mappings,
                 polling_interval, tunnel_types=None):
        """Constructor.

        :param ryuapp: object of the ryu app.
        :param integ_br: name of the integration bridge.
        :param local_ip: local IP address of this hypervisor.
        :param bridge_mappings: mappings from physical network name to bridge.
               (deprecated)
        :param interface_mappings: mappings from physical network name to
               interface.
        :param polling_interval: interval (secs) to poll DB.
        :param tunnel_types: A list of tunnel types to enable support for in
               the agent. If set, will automatically set enable_tunneling to
               True.
        """
        super(OFANeutronAgent, self).__init__()
        self.ryuapp = ryuapp
        # TODO(yamamoto): Remove this VLAN leftover
        self.available_local_vlans = set(range(ofa_const.LOCAL_VLAN_MIN,
                                               ofa_const.LOCAL_VLAN_MAX))
        self.tunnel_types = tunnel_types or []
        l2pop_network_types = list(set(self.tunnel_types +
                                       [p_const.TYPE_VLAN,
                                        p_const.TYPE_FLAT,
                                        p_const.TYPE_LOCAL]))
        self.agent_state = {
            'binary': 'neutron-ofa-agent',
            'host': cfg.CONF.host,
            'topic': n_const.L2_AGENT_TOPIC,
            'configurations': {
                'bridge_mappings': bridge_mappings,
                'interface_mappings': interface_mappings,
                'tunnel_types': self.tunnel_types,
                'tunneling_ip': local_ip,
                'l2_population': True,
                'l2pop_network_types': l2pop_network_types},
            'agent_type': n_const.AGENT_TYPE_OFA,
            'start_flag': True}

        # Keep track of int_br's device count for use by _report_state()
        self.int_br_device_count = 0

        self.int_br = Bridge(integ_br, self.ryuapp)
        # Stores port update notifications for processing in main loop
        self.updated_ports = set()
        self.setup_rpc()
        self.setup_integration_br()
        self.int_ofports = {}
        self.setup_physical_interfaces(interface_mappings)
        self.local_vlan_map = {}
        self.tun_ofports = {}  # network_type -> tunnel ofport
        self.polling_interval = polling_interval

        self.enable_tunneling = bool(self.tunnel_types)
        self.local_ip = local_ip
        self.tunnel_count = 0
        self.vxlan_udp_port = cfg.CONF.AGENT.vxlan_udp_port
        self.dont_fragment = cfg.CONF.AGENT.dont_fragment

        # Security group agent support
        self.sg_agent = sg_rpc.SecurityGroupAgentRpc(self.context,
                self.sg_plugin_rpc, defer_refresh_firewall=True)
        # Initialize iteration counter
        self.iter_num = 0

    def _report_state(self):
        # How many devices are likely used by a VM
        self.agent_state.get('configurations')['devices'] = (
            self.int_br_device_count)
        try:
            self.state_rpc.report_state(self.context,
                                        self.agent_state)
            self.agent_state.pop('start_flag', None)
        except Exception:
            LOG.exception(_LE("Failed reporting state!"))

    def _create_tunnel_port_name(self, tunnel_type):
        return '_ofa-tun-%s' % (tunnel_type,)

    def setup_rpc(self):
        mac = self.int_br.get_local_port_mac()
        self.agent_id = '%s%s' % ('ovs', (mac.replace(":", "")))
        self.topic = topics.AGENT
        self.plugin_rpc = agent_rpc.PluginApi(topics.PLUGIN)
        self.sg_plugin_rpc = sg_rpc.SecurityGroupServerRpcApi(topics.PLUGIN)
        self.state_rpc = agent_rpc.PluginReportStateAPI(topics.PLUGIN)

        # RPC network init
        self.context = context.get_admin_context_without_session()
        # Handle updates from service
        self.endpoints = [self]
        # Define the listening consumers for the agent
        consumers = [[topics.PORT, topics.UPDATE],
                     [topics.SECURITY_GROUP, topics.UPDATE],
                     [topics.L2POPULATION, topics.UPDATE, cfg.CONF.host]]
        self.connection = agent_rpc.create_consumers(self.endpoints,
                                                     self.topic,
                                                     consumers)
        report_interval = cfg.CONF.AGENT.report_interval
        if report_interval:
            heartbeat = loopingcall.FixedIntervalLoopingCall(
                self._report_state)
            heartbeat.start(interval=report_interval)

    def _send_set_async(self, br):
        """Set asynchronous configuration message for the given bridge."""
        datapath = br.datapath
        ofp = datapath.ofproto
        ofpp = datapath.ofproto_parser
        packet_in_mask = 1 << ofp.OFPR_ACTION | 1 << ofp.OFPR_INVALID_TTL
        port_status_mask = 1 << ofp.OFPPR_DELETE
        flow_removed_mask = 0
        msg = ofpp.OFPSetAsync(datapath,
                               [packet_in_mask, 0],
                               [port_status_mask, 0],
                               [flow_removed_mask, 0])
        ryu_api.send_msg(app=self.ryuapp, msg=msg)

    def _get_ports(self, br):
        """Generate ports.Port instances for the given bridge."""
        datapath = br.datapath
        ofpp = datapath.ofproto_parser
        msg = ofpp.OFPPortDescStatsRequest(datapath=datapath)
        descs = ryu_api.send_msg(app=self.ryuapp, msg=msg,
                                 reply_cls=ofpp.OFPPortDescStatsReply,
                                 reply_multi=True)
        for d in descs:
            for p in d.body:
                yield ports.Port.from_ofp_port(p)

    def _get_ofport_names(self, br):
        """Return a set of OpenFlow port names for the given bridge."""
        return set(p.normalized_port_name() for p in
                   self._get_ports(br) if p.is_neutron_port())

    def get_net_uuid(self, vif_id):
        for network_id, vlan_mapping in self.local_vlan_map.items():
            if vif_id in vlan_mapping.vif_ports:
                return network_id

    @log_helpers.log_method_call
    def port_update(self, context, **kwargs):
        port = kwargs.get('port')
        # Put the port identifier in the updated_ports set.
        # Even if full port details might be provided to this call,
        # they are not used since there is no guarantee the notifications
        # are processed in the same order as the relevant API requests
        self.updated_ports.add(ports.get_normalized_port_name(port['id']))

    def _tunnel_port_lookup(self, network_type, _remote_ip):
        return self.tun_ofports.get(network_type)

    @log_helpers.log_method_call
    def fdb_add(self, context, fdb_entries):
        for lvm, agent_ports in self.get_agent_ports(fdb_entries,
                                                     self.local_vlan_map):
            if lvm.network_type in self.tunnel_types:
                local = agent_ports.pop(self.local_ip, None)
                if local:
                    self._fdb_add_arp(lvm, {self.local_ip: local})
                if len(agent_ports):
                    self.fdb_add_tun(context, self.int_br, lvm, agent_ports,
                                     self._tunnel_port_lookup)
            else:
                self._fdb_add_arp(lvm, agent_ports)

    @log_helpers.log_method_call
    def fdb_remove(self, context, fdb_entries):
        for lvm, agent_ports in self.get_agent_ports(fdb_entries,
                                                     self.local_vlan_map):
            if lvm.network_type in self.tunnel_types:
                local = agent_ports.pop(self.local_ip, None)
                if local:
                    self._fdb_remove_arp(lvm, {self.local_ip: local})
                if len(agent_ports):
                    self.fdb_remove_tun(context, self.int_br, lvm, agent_ports,
                                        self._tunnel_port_lookup)
            else:
                self._fdb_remove_arp(lvm, agent_ports)

    @log_helpers.log_method_call
    def _fdb_add_arp(self, lvm, agent_ports):
        for _remote_ip, port_infos in agent_ports.items():
            for port_info in port_infos:
                if port_info == n_const.FLOODING_ENTRY:
                    continue
                self.ryuapp.add_arp_table_entry(lvm.vlan,
                                                port_info.ip_address,
                                                port_info.mac_address)

    @log_helpers.log_method_call
    def _fdb_remove_arp(self, lvm, agent_ports):
        for _remote_ip, port_infos in agent_ports.items():
            for port_info in port_infos:
                if port_info == n_const.FLOODING_ENTRY:
                    continue
                self.ryuapp.del_arp_table_entry(lvm.vlan, port_info.ip_address)

    def add_fdb_flow(self, br, port_info, remote_ip, lvm, ofport):
        assert ofport == self.tun_ofports[lvm.network_type]
        if port_info == n_const.FLOODING_ENTRY:
            lvm.tun_remote_ips.add(remote_ip)
            br.install_tunnel_output(
                tables.TUNNEL_FLOOD[lvm.network_type],
                lvm.vlan, lvm.segmentation_id,
                ofport, lvm.tun_remote_ips, goto_next=True)
        else:
            self.ryuapp.add_arp_table_entry(
                lvm.vlan,
                port_info.ip_address,
                port_info.mac_address)
            br.install_tunnel_output(
                tables.TUNNEL_OUT,
                lvm.vlan, lvm.segmentation_id,
                ofport, set([remote_ip]),
                goto_next=False, eth_dst=port_info.mac_address)

    def del_fdb_flow(self, br, port_info, remote_ip, lvm, ofport):
        assert ofport == self.tun_ofports[lvm.network_type]
        if port_info == n_const.FLOODING_ENTRY:
            if remote_ip not in lvm.tun_remote_ips:
                return  # Ignore unknown addresses
            lvm.tun_remote_ips.remove(remote_ip)
            if len(lvm.tun_remote_ips) > 0:
                br.install_tunnel_output(
                    tables.TUNNEL_FLOOD[lvm.network_type],
                    lvm.vlan, lvm.segmentation_id,
                    ofport, lvm.tun_remote_ips, goto_next=True)
            else:
                br.delete_tunnel_output(
                    tables.TUNNEL_FLOOD[lvm.network_type],
                    lvm.vlan)
        else:
            self.ryuapp.del_arp_table_entry(lvm.vlan, port_info.ip_address)
            br.delete_tunnel_output(tables.TUNNEL_OUT,
                                    lvm.vlan, eth_dst=port_info.mac_address)

    def setup_entry_for_arp_reply(self, br, action, local_vid, mac_address,
                                  ip_address):
        if action == 'add':
            self.ryuapp.add_arp_table_entry(local_vid, ip_address, mac_address)
        elif action == 'remove':
            self.ryuapp.del_arp_table_entry(local_vid, ip_address)

    @log_helpers.log_method_call
    def _fdb_chg_ip(self, context, fdb_entries):
        self.fdb_chg_ip_tun(context, self.int_br, fdb_entries, self.local_ip,
                            self.local_vlan_map)

    def provision_local_vlan(self, net_uuid, network_type, physical_network,
                             segmentation_id):
        """Provisions a local VLAN.

        :param net_uuid: the uuid of the network associated with this vlan.
        :param network_type: the network type ('gre', 'vxlan', 'vlan', 'flat',
                                               'local')
        :param physical_network: the physical network for 'vlan' or 'flat'
        :param segmentation_id: the VID for 'vlan' or tunnel ID for 'tunnel'
        """

        if not self.available_local_vlans:
            LOG.error(_LE("No local VLAN available for net-id=%s"), net_uuid)
            return
        lvid = self.available_local_vlans.pop()
        LOG.info(_LI("Assigning %(vlan_id)s as local vlan for "
                     "net-id=%(net_uuid)s"),
                 {'vlan_id': lvid, 'net_uuid': net_uuid})
        self.local_vlan_map[net_uuid] = LocalVLANMapping(lvid, network_type,
                                                         physical_network,
                                                         segmentation_id)

        if network_type in constants.TUNNEL_NETWORK_TYPES:
            if self.enable_tunneling:
                self.int_br.provision_tenant_tunnel(network_type, lvid,
                                                    segmentation_id)
            else:
                LOG.error(_LE("Cannot provision %(network_type)s network for "
                              "net-id=%(net_uuid)s - tunneling disabled"),
                          {'network_type': network_type,
                           'net_uuid': net_uuid})
        elif network_type in [p_const.TYPE_VLAN, p_const.TYPE_FLAT]:
            if physical_network in self.int_ofports:
                phys_port = self.int_ofports[physical_network]
                self.int_br.provision_tenant_physnet(network_type, lvid,
                                                     segmentation_id,
                                                     phys_port)
            else:
                LOG.error(_LE("Cannot provision %(network_type)s network for "
                              "net-id=%(net_uuid)s - no bridge for "
                              "physical_network %(physical_network)s"),
                          {'network_type': network_type,
                           'net_uuid': net_uuid,
                           'physical_network': physical_network})
        elif network_type == p_const.TYPE_LOCAL:
            # no flows needed for local networks
            pass
        else:
            LOG.error(_LE("Cannot provision unknown network type "
                          "%(network_type)s for net-id=%(net_uuid)s"),
                      {'network_type': network_type,
                       'net_uuid': net_uuid})

    def reclaim_local_vlan(self, net_uuid):
        """Reclaim a local VLAN.

        :param net_uuid: the network uuid associated with this vlan.
        :param lvm: a LocalVLANMapping object that tracks (vlan, lsw_id,
            vif_ids) mapping.
        """
        lvm = self.local_vlan_map.pop(net_uuid, None)
        if lvm is None:
            LOG.debug("Network %s not used on agent.", net_uuid)
            return

        LOG.info(_LI("Reclaiming vlan = %(vlan_id)s from "
                     "net-id = %(net_uuid)s"),
                 {'vlan_id': lvm.vlan,
                  'net_uuid': net_uuid})

        if lvm.network_type in constants.TUNNEL_NETWORK_TYPES:
            if self.enable_tunneling:
                self.int_br.reclaim_tenant_tunnel(lvm.network_type, lvm.vlan,
                                                  lvm.segmentation_id)
        elif lvm.network_type in [p_const.TYPE_FLAT, p_const.TYPE_VLAN]:
            phys_port = self.int_ofports[lvm.physical_network]
            self.int_br.reclaim_tenant_physnet(lvm.network_type, lvm.vlan,
                                               lvm.segmentation_id, phys_port)
        elif lvm.network_type == p_const.TYPE_LOCAL:
            # no flows needed for local networks
            pass
        else:
            LOG.error(_LE("Cannot reclaim unknown network type "
                          "%(network_type)s for net-id=%(net_uuid)s"),
                      {'network_type': lvm.network_type,
                       'net_uuid': net_uuid})

        self.available_local_vlans.add(lvm.vlan)

    def port_bound(self, port, net_uuid,
                   network_type, physical_network, segmentation_id):
        """Bind port to net_uuid/lsw_id and install flow for inbound traffic
        to vm.

        :param port: a ports.Port object.
        :param net_uuid: the net_uuid this port is to be associated with.
        :param network_type: the network type ('gre', 'vlan', 'flat', 'local')
        :param physical_network: the physical network for 'vlan' or 'flat'
        :param segmentation_id: the VID for 'vlan' or tunnel ID for 'tunnel'
        """
        if net_uuid not in self.local_vlan_map:
            self.provision_local_vlan(net_uuid, network_type,
                                      physical_network, segmentation_id)
        lvm = self.local_vlan_map[net_uuid]

        lvm.vif_ports[port.normalized_port_name()] = port
        self.int_br.check_in_port_add_local_port(lvm.vlan, port.ofport)

        # if any of vif mac is unknown, flood unicasts as well
        flood_unicast = any(map(lambda x: x.vif_mac is None,
                                lvm.vif_ports.values()))
        ofports = (vp.ofport for vp in lvm.vif_ports.values())
        self.int_br.local_flood_update(lvm.vlan, ofports, flood_unicast)
        if port.vif_mac is None:
            return
        self.int_br.local_out_add_port(lvm.vlan, port.ofport, port.vif_mac)

    def port_unbound(self, vif_id, net_uuid=None):
        """Unbind port.

        Removes corresponding local vlan mapping object if this is its last
        VIF.

        :param vif_id: the id of the vif
        :param net_uuid: the net_uuid this port is associated with.
        """
        net_uuid = net_uuid or self.get_net_uuid(vif_id)

        if not self.local_vlan_map.get(net_uuid):
            LOG.info(_LI('port_unbound() net_uuid %s not in local_vlan_map'),
                     net_uuid)
            return

        lvm = self.local_vlan_map[net_uuid]
        port = lvm.vif_ports.pop(vif_id, None)

        self.int_br.check_in_port_delete_port(port.ofport)
        if not lvm.vif_ports:
            self.reclaim_local_vlan(net_uuid)
        if port.vif_mac is None:
            return
        self.int_br.local_out_delete_port(lvm.vlan, port.vif_mac)

    def port_dead(self, port, net_uuid=None):
        """Try to stop forwarding on the port.

        :param port: a ports.OFPort object.
        :param net_uuid: network uuid to which the port belongs to.
                         None if unknown.
        """
        self.int_br.check_in_port_delete_port(port.ofport)
        if port.vif_mac is None:
            return
        if net_uuid is None:
            return
        lvm = self.local_vlan_map.get(net_uuid)
        if lvm is not None:
            self.int_br.local_out_delete_port(lvm.vlan, port.vif_mac)

    def setup_integration_br(self):
        """Setup the integration bridge.
        """

        br = self.int_br
        br.setup_ofp()
        br.setup_default_table()
        self._send_set_async(br)

    def setup_physical_interfaces(self, interface_mappings):
        """Setup the physical network interfaces.

        Link physical network interfaces to the integration bridge.

        :param interface_mappings: map physical network names to
                                   interface names.
        """
        for physical_network, interface_name in interface_mappings.items():
            ofport = int(self.int_br.add_port(interface_name))
            self.int_ofports[physical_network] = ofport

    def scan_ports(self, registered_ports, updated_ports=None):
        cur_ports = self._get_ofport_names(self.int_br)
        self.int_br_device_count = len(cur_ports)
        port_info = {'current': cur_ports}
        if updated_ports is None:
            updated_ports = set()
        if updated_ports:
            # Some updated ports might have been removed in the
            # meanwhile, and therefore should not be processed.
            # In this case the updated port won't be found among
            # current ports.
            updated_ports &= cur_ports
            if updated_ports:
                port_info['updated'] = updated_ports

        if cur_ports == registered_ports:
            # No added or removed ports to set, just return here
            return port_info

        port_info['added'] = cur_ports - registered_ports
        # Remove all the known ports not found on the integration bridge
        port_info['removed'] = registered_ports - cur_ports
        return port_info

    def _check_port_status_list(self, port_status_list, port_info):
        """Checks if port_status_list has port-no changed ports.

        Checks if there are ports which port-no change occurred in
        list of port status messages and returns them. And adds target
        ports to an updated set in port_info.

        :param port_status_list: list of port delete status messages
        :param port_info: dict has set of current and updated port names
        """
        # Currently port_status_list has only delete status messages.
        check_ports = dict((ps.name, ps) for ps in port_status_list
            if ps.name in port_info['current'])
        check_ports_set = set(check_ports.keys())
        LOG.debug("Agent daemon_loop check_ports: %(check_ports)s "
                  "check_ports_set: %(check_ports_set)s",
                  {'check_ports': check_ports,
                   'check_ports_set': check_ports_set})
        if check_ports_set:
            if 'updated' in port_info:
                port_info['updated'] |= check_ports_set
            else:
                port_info['updated'] = check_ports_set
        return check_ports

    def treat_vif_port(self, vif_port, port_id, network_id, network_type,
                       physical_network, segmentation_id, admin_state_up):
        if vif_port:
            # When this function is called for a port, the port should have
            # an OVS ofport configured, as only these ports were considered
            # for being treated. If that does not happen, it is a potential
            # error condition of which operators should be aware
            if not vif_port.ofport:
                LOG.warn(_LW("VIF port: %s has no ofport configured, "
                             "and might not be able to transmit"),
                         vif_port.port_name)
            if admin_state_up:
                self.port_bound(vif_port, network_id, network_type,
                                physical_network, segmentation_id)
            else:
                self.port_dead(vif_port, network_id)
        else:
            LOG.debug("No VIF port for port %s defined on agent.", port_id)

    def _setup_tunnel_port(self, br, port_name, tunnel_type):
        # NOTE(yamamoto): Ideally we can specify self.local_ip instead of
        # "0" here.  However, Open vSwitch v2.0.2 doesn't support the
        # specific combination of flow/non-flow parameters we want to use
        # here.  The limitation was removed for Open vSwitch>=v2.3.
        # TODO(yamamoto): Revisit when that version gets available for
        # relevant platforms.
        ofport = br.add_tunnel_port(port_name,
                                    "flow",
                                    "0",
                                    tunnel_type,
                                    self.vxlan_udp_port,
                                    self.dont_fragment)
        if ofport == ovs_lib.INVALID_OFPORT:
            LOG.error(_LE("Failed to set-up %(type)s tunnel port"),
                      {'type': tunnel_type})
            return 0
        ofport = int(ofport)
        self.tun_ofports[tunnel_type] = ofport
        # NOTE(yamamoto): We include local_ip in the match here because
        # our tunnel port is with local_ip=0.  See the above comment.
        br.check_in_port_add_tunnel_port(tunnel_type, ofport, self.local_ip)
        return ofport

    def setup_tunnel_port(self, br, _remote_ip, network_type):
        port_name = self._create_tunnel_port_name(network_type)
        if not port_name:
            return 0
        ofport = self._setup_tunnel_port(br,
                                         port_name,
                                         network_type)
        return ofport

    def cleanup_tunnel_port(self, _br, _tun_ofport, _tunnel_type):
        # Do not bother to remove tunnel ports.
        # We only have one port per network_type.
        pass

    @log_helpers.log_method_call
    def _repair_ofport_change(self, port, net_uuid):
        if net_uuid not in self.local_vlan_map:
            LOG.info(_LI("_repair_ofport_change() net_uuid %s not in "
                         "local_vlan_map"), net_uuid)
            return
        lvm = self.local_vlan_map[net_uuid]
        self.int_br.check_in_port_delete_port(port.ofport)
        if port.vif_mac is None:
            return
        self.int_br.local_out_delete_port(lvm.vlan, port.vif_mac)

    def treat_devices_added_or_updated(self, devices, check_ports):
        resync = False
        all_ports = dict((p.normalized_port_name(), p) for p in
                         self._get_ports(self.int_br) if p.is_neutron_port())
        for device in devices:
            LOG.debug("Processing port %s", device)
            if device not in all_ports:
                # The port has disappeared and should not be processed
                # There is no need to put the port DOWN in the plugin as
                # it never went up in the first place
                LOG.info(_LI("Port %s was not found on the integration bridge "
                             "and will therefore not be processed"), device)
                continue
            port = all_ports[device]
            try:
                details = self.plugin_rpc.get_device_details(self.context,
                                                             device,
                                                             self.agent_id)
            except Exception as e:
                LOG.debug("Unable to get port details for %(device)s: %(e)s",
                          {'device': device, 'e': e})
                resync = True
                continue
            if device in check_ports:
                ps = check_ports[device]
                if port.ofport != ps.port.ofport:
                    ps.port.vif_mac = details.get('mac_address')
                    LOG.debug("Repair ofport changed old port "
                              "ofport: %(ofport)s vif_mac: %(mac)s",
                              {'ofport': ps.port.ofport,
                               'mac': ps.port.vif_mac})
                    LOG.debug("Repair ofport changed new port "
                              "ofport: %(ofport)s vif_mac: %(mac)s",
                              {'ofport': port.ofport,
                               'mac': port.vif_mac})
                    self._repair_ofport_change(ps.port, details['network_id'])
            if 'port_id' in details:
                LOG.info(_LI("Port %(device)s updated. Details: %(details)s"),
                         {'device': device, 'details': details})
                port.vif_mac = details.get('mac_address')
                self.treat_vif_port(port, details['port_id'],
                                    details['network_id'],
                                    details['network_type'],
                                    details['physical_network'],
                                    details['segmentation_id'],
                                    details['admin_state_up'])

                # update plugin about port status
                if details.get('admin_state_up'):
                    LOG.debug("Setting status for %s to UP", device)
                    self.plugin_rpc.update_device_up(
                        self.context, device, self.agent_id, cfg.CONF.host)
                else:
                    LOG.debug("Setting status for %s to DOWN", device)
                    self.plugin_rpc.update_device_down(
                        self.context, device, self.agent_id, cfg.CONF.host)
                LOG.info(_LI("Configuration for device %s completed."), device)
            else:
                LOG.warn(_LW("Device %s not defined on plugin"), device)
                if (port and port.ofport != -1):
                    self.port_dead(port)
        return resync

    def treat_devices_removed(self, devices):
        resync = False
        self.sg_agent.remove_devices_filter(devices)
        for device in devices:
            LOG.info(_LI("Attachment %s removed"), device)
            try:
                self.plugin_rpc.update_device_down(self.context,
                                                   device,
                                                   self.agent_id,
                                                   cfg.CONF.host)
            except Exception as e:
                LOG.debug("port_removed failed for %(device)s: %(e)s",
                          {'device': device, 'e': e})
                resync = True
                continue
            self.port_unbound(device)
        return resync

    def process_network_ports(self, port_info, check_ports):
        resync_add = False
        resync_removed = False
        # If there is an exception while processing security groups ports
        # will not be wired anyway, and a resync will be triggered
        self.sg_agent.setup_port_filters(port_info.get('added', set()),
                                         port_info.get('updated', set()))
        # VIF wiring needs to be performed always for 'new' devices.
        # For updated ports, re-wiring is not needed in most cases, but needs
        # to be performed anyway when the admin state of a device is changed.
        # A device might be both in the 'added' and 'updated'
        # list at the same time; avoid processing it twice.
        devices_added_updated = (port_info.get('added', set()) |
                                 port_info.get('updated', set()))
        if devices_added_updated:
            start = time.time()
            resync_add = self.treat_devices_added_or_updated(
                devices_added_updated, check_ports)
            LOG.debug("process_network_ports - iteration:%(iter_num)d - "
                      "treat_devices_added_or_updated completed "
                      "in %(elapsed).3f",
                      {'iter_num': self.iter_num,
                       'elapsed': time.time() - start})
        if 'removed' in port_info:
            start = time.time()
            resync_removed = self.treat_devices_removed(port_info['removed'])
            LOG.debug("process_network_ports - iteration:%(iter_num)d - "
                      "treat_devices_removed completed in %(elapsed).3f",
                      {'iter_num': self.iter_num,
                       'elapsed': time.time() - start})
        # If one of the above opertaions fails => resync with plugin
        return (resync_add | resync_removed)

    def tunnel_sync(self):
        resync = False
        try:
            for tunnel_type in self.tunnel_types:
                self.plugin_rpc.tunnel_sync(self.context,
                                            self.local_ip,
                                            tunnel_type,
                                            cfg.CONF.host)
        except Exception as e:
            LOG.debug("Unable to sync tunnel IP %(local_ip)s: %(e)s",
                      {'local_ip': self.local_ip, 'e': e})
            resync = True
        return resync

    def _port_info_has_changes(self, port_info):
        return (port_info.get('added') or
                port_info.get('removed') or
                port_info.get('updated'))

    def daemon_loop(self):
        # TODO(yamamoto):
        # It might be better to monitor port status async messages

        sync = True
        ports = set()
        tunnel_sync = True
        while True:
            start = time.time()
            port_stats = {'regular': {'added': 0, 'updated': 0, 'removed': 0}}
            LOG.debug("Agent daemon_loop - iteration:%d started",
                      self.iter_num)
            if sync:
                LOG.info(_LI("Agent out of sync with plugin!"))
                ports.clear()
                sync = False
            # Notify the plugin of tunnel IP
            if self.enable_tunneling and tunnel_sync:
                LOG.info(_LI("Agent tunnel out of sync with plugin!"))
                try:
                    tunnel_sync = self.tunnel_sync()
                except Exception:
                    LOG.exception(_LE("Error while synchronizing tunnels"))
                    tunnel_sync = True
            LOG.debug("Agent daemon_loop - iteration:%(iter_num)d - "
                      "starting polling. Elapsed:%(elapsed).3f",
                      {'iter_num': self.iter_num,
                       'elapsed': time.time() - start})
            try:
                # Save updated ports dict to perform rollback in
                # case resync would be needed, and then clear
                # self.updated_ports. As the greenthread should not yield
                # between these two statements, this will be thread-safe
                updated_ports_copy = self.updated_ports
                self.updated_ports = set()
                port_info = self.scan_ports(ports, updated_ports_copy)
                ports = port_info['current']
                # In the following calling port_info may be updated
                port_status_list = self.ryuapp.get_port_status_list()
                check_ports = self._check_port_status_list(port_status_list,
                                                           port_info)
                LOG.debug("Agent daemon_loop - iteration:%(iter_num)d - "
                          "port information retrieved. "
                          "Elapsed:%(elapsed).3f",
                          {'iter_num': self.iter_num,
                           'elapsed': time.time() - start})
                # Secure and wire/unwire VIFs and update their status
                # on Neutron server
                if (self._port_info_has_changes(port_info) or
                    self.sg_agent.firewall_refresh_needed()):
                    LOG.debug("Starting to process devices in:%s",
                              port_info)
                    # If treat devices fails - must resync with plugin
                    sync = self.process_network_ports(port_info, check_ports)
                    LOG.debug("Agent daemon_loop - "
                              "iteration:%(iter_num)d - "
                              "ports processed. Elapsed:%(elapsed).3f",
                              {'iter_num': self.iter_num,
                               'elapsed': time.time() - start})
                    port_stats['regular']['added'] = (
                        len(port_info.get('added', [])))
                    port_stats['regular']['updated'] = (
                        len(port_info.get('updated', [])))
                    port_stats['regular']['removed'] = (
                        len(port_info.get('removed', [])))
            except Exception:
                LOG.exception(_LE("Error while processing VIF ports"))
                # Put the ports back in self.updated_port
                self.updated_ports |= updated_ports_copy
                sync = True

            # sleep till end of polling interval
            elapsed = (time.time() - start)
            LOG.debug("Agent daemon_loop - iteration:%(iter_num)d "
                      "completed. Processed ports statistics:"
                      "%(port_stats)s. Elapsed:%(elapsed).3f",
                      {'iter_num': self.iter_num,
                       'port_stats': port_stats,
                       'elapsed': elapsed})
            if (elapsed < self.polling_interval):
                time.sleep(self.polling_interval - elapsed)
            else:
                LOG.debug("Loop iteration exceeded interval "
                          "(%(polling_interval)s vs. %(elapsed)s)!",
                          {'polling_interval': self.polling_interval,
                           'elapsed': elapsed})
            self.iter_num = self.iter_num + 1


def create_agent_config_map(config):
    """Create a map of agent config parameters.

    :param config: an instance of cfg.CONF
    :returns: a map of agent configuration parameters
    """
    try:
        bridge_mappings = n_utils.parse_mappings(config.OVS.bridge_mappings)
    except ValueError as e:
        raise ValueError(_("Parsing bridge_mappings failed: %s.") % e)
    try:
        interface_mappings = n_utils.parse_mappings(
            config.AGENT.physical_interface_mappings)
    except ValueError as e:
        raise ValueError(_("Parsing physical_interface_mappings failed: %s.")
                         % e)

    kwargs = dict(
        integ_br=config.OVS.integration_bridge,
        local_ip=config.OVS.local_ip,
        interface_mappings=interface_mappings,
        bridge_mappings=bridge_mappings,
        polling_interval=config.AGENT.polling_interval,
        tunnel_types=config.AGENT.tunnel_types,
    )

    # Verify the tunnel_types specified are valid
    for tun in kwargs['tunnel_types']:
        if tun not in constants.TUNNEL_NETWORK_TYPES:
            msg = _('Invalid tunnel type specificed: %s'), tun
            raise ValueError(msg)
        if not kwargs['local_ip']:
            msg = _('Tunneling cannot be enabled without a valid local_ip.')
            raise ValueError(msg)

    return kwargs
