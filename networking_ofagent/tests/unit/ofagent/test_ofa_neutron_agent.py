# Copyright (C) 2014,2015 VA Linux Systems Japan K.K.
# Copyright (C) 2014,2015 Fumihiko Kakuma <kakuma at valinux co jp>
# Copyright (C) 2014 YAMAMOTO Takashi <yamamoto at valinux co jp>
# All Rights Reserved.
#
# Based on test for openvswitch agent(test_ovs_neutron_agent.py).
#
# Copyright (c) 2012 OpenStack Foundation.
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

import collections
import contextlib
import copy

import mock
from oslo_config import cfg
from oslo_utils import importutils
import testtools

from neutron.agent.common import ovs_lib
from neutron.common import constants as n_const
from neutron.plugins.common import constants as p_const
from neutron.plugins.ml2.drivers.l2pop import rpc as l2pop_rpc

from networking_ofagent.plugins.ofagent.agent.monitor import PortStatus
from networking_ofagent.tests.unit.ofagent import ofa_test_base


NOTIFIER = ('neutron.plugins.ml2.rpc.AgentNotifierApi')
FLOODING_ENTRY = l2pop_rpc.PortInfo(*n_const.FLOODING_ENTRY)


def _mock_port(is_neutron=True, normalized_name=None):
    p = mock.Mock()
    p.is_neutron_port.return_value = is_neutron
    if normalized_name:
        p.normalized_port_name.return_value = normalized_name
    return p


def _make_portstatus(name, reason):
    port = _mock_port(True, name)
    return PortStatus(reason=reason, port=port, name=name)


class CreateAgentConfigMap(ofa_test_base.OFAAgentTestBase):

    def test_create_agent_config_map_succeeds(self):
        self.assertTrue(self.mod_agent.create_agent_config_map(cfg.CONF))

    def test_create_agent_config_map_fails_for_invalid_tunnel_config(self):
        # An ip address is required for tunneling but there is no default,
        # verify this for both gre and vxlan tunnels.
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_no_local_ip(self):
        # An ip address is required for tunneling but there is no default
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_VXLAN],
                              group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_fails_for_invalid_tunnel_type(self):
        cfg.CONF.set_override('tunnel_types', ['foobar'], group='AGENT')
        with testtools.ExpectedException(ValueError):
            self.mod_agent.create_agent_config_map(cfg.CONF)

    def test_create_agent_config_map_multiple_tunnel_types(self):
        cfg.CONF.set_override('local_ip', '10.10.10.10', group='OVS')
        cfg.CONF.set_override('tunnel_types', [p_const.TYPE_GRE,
                              p_const.TYPE_VXLAN], group='AGENT')
        cfgmap = self.mod_agent.create_agent_config_map(cfg.CONF)
        self.assertEqual(cfgmap['tunnel_types'],
                         [p_const.TYPE_GRE, p_const.TYPE_VXLAN])


class TestOFANeutronAgentBridge(ofa_test_base.OFAAgentTestBase):

    def setUp(self):
        super(TestOFANeutronAgentBridge, self).setUp()
        self.br_name = 'bridge1'
        self.ovs = self.mod_agent.Bridge(
            self.br_name, self.ryuapp)

    def test_find_datapath_id(self):
        with mock.patch.object(self.ovs, 'get_datapath_id',
                               return_value='12345'):
            self.ovs.find_datapath_id()
        self.assertEqual(self.ovs.datapath_id, '12345')

    def _fake_get_datapath(self, app, datapath_id):
        if self.ovs.retry_count >= 2:
            datapath = mock.Mock()
            datapath.ofproto_parser = mock.Mock()
            return datapath
        self.ovs.retry_count += 1
        return None

    def test_get_datapath_normal(self):
        self.ovs.retry_count = 0
        with mock.patch.object(self.mod_agent.ryu_api, 'get_datapath',
                               new=self._fake_get_datapath):
            self.ovs.datapath_id = '0x64'
            self.ovs.get_datapath(retry_max=4)
        self.assertEqual(self.ovs.retry_count, 2)

    def test_get_datapath_retry_out_by_default_time(self):
        cfg.CONF.set_override('get_datapath_retry_times', 3, group='AGENT')
        with mock.patch.object(self.mod_agent.ryu_api, 'get_datapath',
                               return_value=None) as mock_get_datapath:
            with testtools.ExpectedException(SystemExit):
                self.ovs.datapath_id = '0x64'
                self.ovs.get_datapath(retry_max=3)
        self.assertEqual(mock_get_datapath.call_count, 3)

    def test_get_datapath_retry_out_by_specified_time(self):
        with mock.patch.object(self.mod_agent.ryu_api, 'get_datapath',
                               return_value=None) as mock_get_datapath:
            with testtools.ExpectedException(SystemExit):
                self.ovs.datapath_id = '0x64'
                self.ovs.get_datapath(retry_max=2)
        self.assertEqual(mock_get_datapath.call_count, 2)

    def test_setup_ofp_default_par(self):
        with contextlib.nested(
            mock.patch.object(self.ovs, 'set_protocols'),
            mock.patch.object(self.ovs, 'set_controller'),
            mock.patch.object(self.ovs, 'find_datapath_id'),
            mock.patch.object(self.ovs, 'get_datapath'),
        ) as (mock_set_protocols, mock_set_controller,
              mock_find_datapath_id, mock_get_datapath):
            self.ovs.setup_ofp()
        mock_set_protocols.assert_called_with('OpenFlow13')
        mock_set_controller.assert_called_with(['tcp:127.0.0.1:6633'])
        mock_get_datapath.assert_called_with(
            cfg.CONF.AGENT.get_datapath_retry_times)
        self.assertEqual(mock_find_datapath_id.call_count, 1)

    def test_setup_ofp_specify_par(self):
        controller_names = ['tcp:192.168.10.10:1234', 'tcp:172.17.16.20:5555']
        with contextlib.nested(
            mock.patch.object(self.ovs, 'set_protocols'),
            mock.patch.object(self.ovs, 'set_controller'),
            mock.patch.object(self.ovs, 'find_datapath_id'),
            mock.patch.object(self.ovs, 'get_datapath'),
        ) as (mock_set_protocols, mock_set_controller,
              mock_find_datapath_id, mock_get_datapath):
            self.ovs.setup_ofp(controller_names=controller_names,
                               protocols='OpenFlow133',
                               retry_max=11)
        mock_set_protocols.assert_called_with('OpenFlow133')
        mock_set_controller.assert_called_with(controller_names)
        mock_get_datapath.assert_called_with(11)
        self.assertEqual(mock_find_datapath_id.call_count, 1)

    def test_setup_ofp_with_except(self):
        with contextlib.nested(
            mock.patch.object(self.ovs, 'set_protocols',
                              side_effect=RuntimeError),
            mock.patch.object(self.ovs, 'set_controller'),
            mock.patch.object(self.ovs, 'find_datapath_id'),
            mock.patch.object(self.ovs, 'get_datapath'),
        ) as (mock_set_protocols, mock_set_controller,
              mock_find_datapath_id, mock_get_datapath):
            with testtools.ExpectedException(SystemExit):
                self.ovs.setup_ofp()


class TestOFANeutronAgent(ofa_test_base.OFAAgentTestBase):

    def setUp(self):
        super(TestOFANeutronAgent, self).setUp()
        notifier_p = mock.patch(NOTIFIER)
        notifier_cls = notifier_p.start()
        self.notifier = mock.Mock()
        notifier_cls.return_value = self.notifier
        kwargs = self.mod_agent.create_agent_config_map(cfg.CONF)

        class MockFixedIntervalLoopingCall(object):
            def __init__(self, f):
                self.f = f

            def start(self, interval=0):
                self.f()

        with contextlib.nested(
            mock.patch.object(self.mod_agent.OFANeutronAgent,
                              'setup_integration_br',
                              return_value=mock.Mock()),
            mock.patch.object(self.mod_agent.Bridge,
                              'get_local_port_mac',
                              return_value='00:00:00:00:00:01'),
            mock.patch('neutron.agent.linux.utils.get_interface_mac',
                       return_value='00:00:00:00:00:01'),
            mock.patch('neutron.openstack.common.loopingcall.'
                       'FixedIntervalLoopingCall',
                       new=MockFixedIntervalLoopingCall)):
            self.agent = self.mod_agent.OFANeutronAgent(self.ryuapp, **kwargs)

        self.agent.sg_agent = mock.Mock()
        self.int_dp = self._mk_test_dp('int_br')
        self.agent.int_br = self._mk_test_br('int_br')
        self.agent.int_br.set_dp(self.int_dp)
        self.agent.int_ofports['phys-net1'] = 666

    def _create_tunnel_port_name(self, tunnel_type):
        return '_ofa-tun-%s' % tunnel_type

    def mock_scan_ports(self, port_set=None, registered_ports=None,
                        updated_ports=None, port_tags_dict=None):
        port_tags_dict = port_tags_dict or {}
        with contextlib.nested(
            mock.patch.object(self.agent, '_get_ofport_names',
                              return_value=port_set),
            mock.patch.object(self.agent.int_br, 'get_port_tag_dict',
                              return_value=port_tags_dict)
        ):
            return self.agent.scan_ports(registered_ports, updated_ports)

    def test_scan_ports_returns_current_only_for_unchanged_ports(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 3])
        expected = {'current': vif_port_set}
        actual = self.mock_scan_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_returns_port_changes(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]), removed=set([2]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports)
        self.assertEqual(expected, actual)

    def _test_scan_ports_with_updated_ports(self, updated_ports):
        vif_port_set = set([1, 3, 4])
        registered_ports = set([1, 2, 4])
        expected = dict(current=vif_port_set, added=set([3]),
                        removed=set([2]), updated=set([4]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_finds_known_updated_ports(self):
        self._test_scan_ports_with_updated_ports(set([4]))

    def test_scan_ports_ignores_unknown_updated_ports(self):
        # the port '5' was not seen on current ports. Hence it has either
        # never been wired or already removed and should be ignored
        self._test_scan_ports_with_updated_ports(set([4, 5]))

    def test_scan_ports_ignores_updated_port_if_removed(self):
        vif_port_set = set([1, 3])
        registered_ports = set([1, 2])
        updated_ports = set([1, 2])
        expected = dict(current=vif_port_set, added=set([3]),
                        removed=set([2]), updated=set([1]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_scan_ports_no_vif_changes_returns_updated_port_only(self):
        vif_port_set = set([1, 2, 3])
        registered_ports = set([1, 2, 3])
        updated_ports = set([2])
        expected = dict(current=vif_port_set, updated=set([2]))
        actual = self.mock_scan_ports(vif_port_set, registered_ports,
                                      updated_ports)
        self.assertEqual(expected, actual)

    def test_treat_devices_added_returns_true_for_missing_device(self):
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                              side_effect=Exception()),
            mock.patch.object(self.agent, '_get_ports',
                              return_value=[_mock_port(True, 'xxx')])):
            self.assertTrue(self.agent.treat_devices_added_or_updated(
                ['xxx'], {}))

    def test__repair_ofport_change(self):
        port = mock.Mock()
        port.ofport = '12'
        port.vif_mac = 'fa:16:3e:d7:0a:d3'
        net_id = '00a622b1-91a5-4482-8c2f-9f2820957f18'
        lvm = mock.Mock()
        lvm.vlan = '1001'
        self.agent.local_vlan_map[net_id] = lvm
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'check_in_port_delete_port'),
            mock.patch.object(self.agent.int_br, 'local_out_delete_port')
        ) as (in_port_del, out_del):
            self.agent._repair_ofport_change(port, net_id)
            in_port_del.assert_called_once_with(port.ofport)
            out_del.assert_called_once_with(lvm.vlan, port.vif_mac)

    def _mock_treat_devices_added_updated(self, details, port, all_ports,
                                          check_ports, func_name):
        """Mock treat devices added or updated.

        :param details: the details to return for the device
        :param port: port name to process
        :param all_ports: the port that _get_ports return
        :param func_name: the function that should be called
        :returns: whether the named function was called
        """
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                              return_value=details),
            mock.patch.object(self.agent, '_get_ports',
                              return_value=all_ports),
            mock.patch.object(self.agent, '_repair_ofport_change'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, func_name)
        ) as (get_dev_fn, _get_ports, _repair_ofport, upd_dev_up,
              upd_dev_down, func):
            self.assertFalse(self.agent.treat_devices_added_or_updated(
                [port], check_ports))
        _get_ports.assert_called_once_with(self.agent.int_br)
        return func.called

    def test_treat_devices_added_updated_ignores_invalid_ofport(self):
        port_name = 'hoge'
        p1 = _mock_port(True, port_name)
        p1.ofport = -1
        self.assertFalse(self._mock_treat_devices_added_updated(
            mock.MagicMock(), port_name, [p1], {}, 'port_dead'))

    def test_treat_devices_added_updated_marks_unknown_port_as_dead(self):
        port_name = 'hoge'
        p1 = _mock_port(True, port_name)
        p1.ofport = 1
        self.assertTrue(self._mock_treat_devices_added_updated(
            mock.MagicMock(), port_name, [p1], {}, 'port_dead'))

    def test_treat_devices_added_does_not_process_missing_port(self):
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details'),
            mock.patch.object(self.agent.int_br, 'get_vif_port_by_id',
                              return_value=None)
        ) as (get_dev_fn, get_vif_func):
            self.assertFalse(get_dev_fn.called)

    def test_treat_devices_added_updated_updates_known_port(self):
        port_name = 'tapd3315981-0b'
        p1 = _mock_port(False)
        p2 = _mock_port(True, port_name)
        ports = [p1, p2]
        details = mock.MagicMock()
        details.__contains__.side_effect = lambda x: True
        self.assertTrue(self._mock_treat_devices_added_updated(
            details, port_name, ports, {}, 'treat_vif_port'))

    def test_treat_devices_added_updated_put_port_down(self):
        fake_details_dict = {'admin_state_up': False,
                             'port_id': 'xxx',
                             'device': 'xxx',
                             'network_id': 'yyy',
                             'physical_network': 'foo',
                             'segmentation_id': 'bar',
                             'network_type': 'baz'}
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                              return_value=fake_details_dict),
            mock.patch.object(self.agent, '_get_ports',
                              return_value=[_mock_port(True, 'xxx')]),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'treat_vif_port')
        ) as (get_dev_fn, _get_ports, upd_dev_up,
              upd_dev_down, treat_vif_port):
            self.assertFalse(self.agent.treat_devices_added_or_updated(
                ['xxx'], {}))
            self.assertTrue(treat_vif_port.called)
            self.assertTrue(upd_dev_down.called)
        _get_ports.assert_called_once_with(self.agent.int_br)

    def test_treat_devices_added_updated_repair_flow(self):
        name1 = 'tapd3315981-0b'
        name2 = 'tapd3315982-0b'
        net_id = '00a622b1-91a5-4482-8c2f-9f2820957f18'
        devices = [name1, name2]
        p1 = _mock_port(True, name1)
        p2 = _mock_port(True, name2)
        all_ports = [p1, p2]
        details = {'admin_state_up': True,
                   'port_id': '123',
                   'device': '456',
                   'network_id': net_id,
                   'physical_network': None,
                   'segmentation_id': '1001',
                   'network_type': 'vxlan'}
        ps2 = _make_portstatus('tapd3315982-0b', 'mod')
        check_ports = {name2: ps2}
        with contextlib.nested(
            mock.patch.object(self.agent.plugin_rpc, 'get_device_details',
                              return_value=details),
            mock.patch.object(self.agent, '_get_ports',
                              return_value=all_ports),
            mock.patch.object(self.agent, '_repair_ofport_change'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_up'),
            mock.patch.object(self.agent.plugin_rpc, 'update_device_down'),
            mock.patch.object(self.agent, 'treat_vif_port')
        ) as (get_dev_fn, _get_ports, _repair_ofport, upd_dev_up,
              upd_dev_down, treat_vif_port):
            self.assertFalse(self.agent.treat_devices_added_or_updated(
                devices, check_ports))
        _repair_ofport.assert_called_once_with(ps2.port, net_id)

    def test_treat_devices_removed_returns_true_for_missing_device(self):
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               side_effect=Exception()):
            self.assertTrue(self.agent.treat_devices_removed([{}]))

    def _mock_treat_devices_removed(self, port_exists):
        details = dict(exists=port_exists)
        with mock.patch.object(self.agent.plugin_rpc, 'update_device_down',
                               return_value=details):
            with mock.patch.object(self.agent, 'port_unbound') as port_unbound:
                self.assertFalse(self.agent.treat_devices_removed([{}]))
        self.assertTrue(port_unbound.called)

    def test_treat_devices_removed_unbinds_port(self):
        self._mock_treat_devices_removed(True)

    def test_treat_devices_removed_ignores_missing_port(self):
        self._mock_treat_devices_removed(False)

    def test__check_port_status_list_no_ofport_change(self):
        port_status_list = [_make_portstatus('tapd3315981-0b', 'del'),
                            _make_portstatus('tapd3315982-0b', 'del'),
                            _make_portstatus('tapd3315983-0b', 'del')]
        port_info = {'current': set()}
        check_ports = self.agent._check_port_status_list(port_status_list,
                                                         port_info)
        self.assertFalse(check_ports)
        self.assertEqual(port_info, {'current': set()})

    def test__check_port_status_list_ofport_change(self):
        name1 = 'tapd3315981-0b'
        name3 = 'tapd3315983-0b'
        port_status_list = [_make_portstatus(name1, 'del'),
                            _make_portstatus('tapd3315982-0b', 'del'),
                            _make_portstatus(name3, 'add')]
        port_info = {'current': set([name1, name3])}
        expected_check_ports = {name1: port_status_list[0],
                                name3: port_status_list[2]}
        expected_port_info = {'current': set([name1, name3]),
                              'updated': set([name1, name3])}
        check_ports = self.agent._check_port_status_list(port_status_list,
                                                         port_info)
        self.assertEqual(check_ports, expected_check_ports)
        self.assertEqual(port_info, expected_port_info)

    def test__check_port_status_list_extend_updated_ports(self):
        name1 = 'tapd3315981-0b'
        name3 = 'tapd3315983-0b'
        name4 = 'tapd3315984-0b'
        port_status_list = [_make_portstatus(name1, 'del'),
                            _make_portstatus('tapd3315982-0b', 'del'),
                            _make_portstatus(name3, 'add')]
        port_info = {'current': set([name1, name3]),
                     'updated': set([name4])}
        expected_check_ports = {name1: port_status_list[0],
                                name3: port_status_list[2]}
        expected_port_info = {'current': set([name1, name3]),
                              'updated': set([name4, name1, name3])}
        check_ports = self.agent._check_port_status_list(port_status_list,
                                                         port_info)
        self.assertEqual(check_ports, expected_check_ports)
        self.assertEqual(port_info, expected_port_info)

    def _test_process_network_ports(self, port_info, check_ports):
        with contextlib.nested(
            mock.patch.object(self.agent.sg_agent, "setup_port_filters"),
            mock.patch.object(self.agent, "treat_devices_added_or_updated",
                              return_value=False),
            mock.patch.object(self.agent, "treat_devices_removed",
                              return_value=False)
        ) as (setup_port_filters, device_added_updated, device_removed):
            self.assertFalse(self.agent.process_network_ports(port_info,
                                                              check_ports))
            setup_port_filters.assert_called_once_with(
                port_info['added'], port_info.get('updated', set()))
            device_added_updated.assert_called_once_with(
                port_info['added'] | port_info.get('updated', set()),
                check_ports)
            device_removed.assert_called_once_with(port_info['removed'])

    def test_process_network_ports(self):
        self._test_process_network_ports(
            {'current': set(['tap0']),
             'removed': set(['eth0']),
             'added': set(['eth1'])},
            {})

    def test_process_network_port_with_updated_ports(self):
        self._test_process_network_ports(
            {'current': set(['tap0', 'tap1']),
             'updated': set(['tap1', 'eth1']),
             'removed': set(['eth0']),
             'added': set(['eth1'])},
            {})

    def test_report_state(self):
        with mock.patch.object(self.agent.state_rpc,
                               "report_state") as report_st:
            self.agent.int_br_device_count = 5
            self.agent._report_state()
            report_st.assert_called_with(self.agent.context,
                                         self.agent.agent_state)
            self.assertNotIn("start_flag", self.agent.agent_state)
            self.assertEqual(
                self.agent.agent_state["configurations"]["devices"],
                self.agent.int_br_device_count
            )

    def test_port_update(self):
        port = {"id": "b1981919-f516-11e3-a8f4-08606e7f74e7",
                "network_id": "124",
                "admin_state_up": False}
        self.agent.port_update("unused_context",
                               port=port,
                               network_type="vlan",
                               segmentation_id="1",
                               physical_network="physnet")
        self.assertEqual(set(['tapb1981919-f5']), self.agent.updated_ports)

    def test_setup_physical_interfaces(self):
        with mock.patch.object(self.agent.int_br, "add_port") as add_port_fn:
            add_port_fn.return_value = "111"
            self.agent.setup_physical_interfaces({"physnet1": "eth1"})
            add_port_fn.assert_called_once_with("eth1")
            self.assertEqual(111, self.agent.int_ofports["physnet1"])

    def test_port_unbound(self):
        with contextlib.nested(
            mock.patch.object(self.agent, "reclaim_local_vlan"),
            mock.patch.object(self.agent, "get_net_uuid",
                              return_value="netuid12345"),
        ) as (reclvl_fn, _):
            self.agent.enable_tunneling = True
            lvm = mock.Mock()
            lvm.network_type = "gre"
            lvm.vif_ports = {"vif1": mock.Mock()}
            self.agent.local_vlan_map["netuid12345"] = lvm
            self.agent.port_unbound("vif1")
            self.assertTrue(reclvl_fn.called)

    def _prepare_l2_pop_ofports(self, network_type=None):
        LVM = collections.namedtuple('LVM', 'net, vlan, segid, ip')
        self.lvms = [LVM(net='net1', vlan=11, segid=21, ip='1.1.1.1'),
                     LVM(net='net2', vlan=12, segid=22, ip='2.2.2.2')]
        self.tunnel_type = 'gre'
        self.tun_name = self._create_tunnel_port_name(self.tunnel_type)
        if network_type is None:
            network_type = self.tunnel_type
        lvm1 = mock.Mock()
        lvm1.network_type = network_type
        lvm1.vlan = self.lvms[0].vlan
        lvm1.segmentation_id = self.lvms[0].segid
        lvm1.tun_remote_ips = set(self.lvms[x].ip for x in [0])
        lvm2 = mock.Mock()
        lvm2.network_type = network_type
        lvm2.vlan = self.lvms[1].vlan
        lvm2.segmentation_id = self.lvms[1].segid
        lvm2.tun_remote_ips = set(self.lvms[x].ip for x in [0, 1])
        self.agent.tunnel_types = [self.tunnel_type]
        self.agent.local_vlan_map = {self.lvms[0].net: lvm1,
                                     self.lvms[1].net: lvm2}
        self.agent.tun_ofports = {self.tunnel_type: 1}

    def test_fdb_ignore_network(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {'net3': {}}
        with contextlib.nested(
            mock.patch.object(self.agent, '_setup_tunnel_port'),
            mock.patch.object(self.agent, 'cleanup_tunnel_port')
        ) as (add_tun_fn, clean_tun_fn):
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)
            self.agent.fdb_remove(None, fdb_entry)
            self.assertFalse(clean_tun_fn.called)

    def test_fdb_ignore_self(self):
        self._prepare_l2_pop_ofports()
        self.agent.local_ip = 'agent_ip'
        fdb_entry = {self.lvms[1].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun2',
                      'ports':
                      {'agent_ip':
                       [l2pop_rpc.PortInfo('mac', 'ip'),
                        FLOODING_ENTRY]}}}
        with contextlib.nested(
            mock.patch.object(self.agent.ryuapp, "add_arp_table_entry"),
            mock.patch.object(self.agent.ryuapp, "del_arp_table_entry"),
        ) as (add_fn, del_fn):
            self.agent.fdb_add(None, copy.deepcopy(fdb_entry))
            add_fn.assert_called_once_with(12, 'ip', 'mac')
            self.assertFalse(del_fn.called)
            self.agent.fdb_remove(None, fdb_entry)
            add_fn.assert_called_once_with(12, 'ip', 'mac')
            del_fn.assert_called_once_with(12, 'ip')

    def test_fdb_add_flows(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {self.lvms[0].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun1',
                      'ports':
                      {self.lvms[1].ip:
                       [l2pop_rpc.PortInfo('mac', 'ip'),
                        FLOODING_ENTRY]}}}
        with contextlib.nested(
            mock.patch.object(self.agent, '_setup_tunnel_port'),
            mock.patch.object(self.agent.int_br, 'install_tunnel_output'),
            mock.patch.object(self.agent.int_br, 'delete_tunnel_output'),
        ) as (add_tun_fn, install_fn, delete_fn):
            add_tun_fn.return_value = 2
            self.agent.fdb_add(None, fdb_entry)
            self.assertEqual(2, install_fn.call_count)
            expected_calls = [
                mock.call(7, 11, 21, 1, set(self.lvms[x].ip for x in [1]),
                          eth_dst='mac', goto_next=False),
                mock.call(10, 11, 21, 1, set(self.lvms[x].ip for x in [0, 1]),
                          goto_next=True)
            ]
            install_fn.assert_has_calls(expected_calls)
            self.assertFalse(delete_fn.called)

    def test_fdb_del_flows(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {self.lvms[1].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun2',
                      'ports':
                      {self.lvms[1].ip:
                       [l2pop_rpc.PortInfo('mac', 'ip'),
                        FLOODING_ENTRY]}}}
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'install_tunnel_output'),
            mock.patch.object(self.agent.int_br, 'delete_tunnel_output'),
        ) as (install_fn, delete_fn):
            self.agent.fdb_remove(None, fdb_entry)
            install_fn.assert_called_once_with(10, 12, 22, 1,
                                               set([self.lvms[0].ip]),
                                               goto_next=True)
            delete_fn.assert_called_once_with(7, 12, eth_dst='mac')

    def test_fdb_add_port(self):
        self._prepare_l2_pop_ofports()
        tunnel_ip = '10.10.10.10'
        fdb_entry = {self.lvms[0].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun1',
                      'ports': {self.lvms[0].ip: [l2pop_rpc.PortInfo('mac',
                                                                     'ip')]}}}
        with mock.patch.object(self.agent, '_setup_tunnel_port') as add_tun_fn:
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)
            fdb_entry[self.lvms[0].net]['ports'][tunnel_ip] = [
                l2pop_rpc.PortInfo('mac', 'ip')]
            self.agent.fdb_add(None, fdb_entry)
            self.assertFalse(add_tun_fn.called)

    def test_fdb_del_port(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {self.lvms[1].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun2',
                      'ports': {self.lvms[1].ip: [FLOODING_ENTRY]}}}
        with mock.patch.object(self.agent.int_br,
                               'delete_port') as del_port_fn:
            self.agent.fdb_remove(None, fdb_entry)
            self.assertFalse(del_port_fn.called)

    def test_add_arp_table_entry(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {self.lvms[0].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun1',
                      'ports': {self.lvms[0].ip: [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac1', 'ip1')],
                                self.lvms[1].ip: [
                                    l2pop_rpc.PortInfo('mac2', 'ip2')],
                                '192.0.2.1': [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac3', 'ip3')]}}}
        with mock.patch.object(self.agent,
                               'setup_tunnel_port') as setup_tun_fn:
            self.agent.fdb_add(None, fdb_entry)
            calls = [
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip1', 'mac1'),
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip2', 'mac2')
            ]
            self.ryuapp.add_arp_table_entry.assert_has_calls(calls,
                                                             any_order=True)
            self.assertFalse(setup_tun_fn.called)

    def _test_add_arp_table_entry_non_tunnel(self, network_type):
        self._prepare_l2_pop_ofports(network_type=network_type)
        fdb_entry = {self.lvms[0].net:
                     {'network_type': network_type,
                      'segment_id': 'tun1',
                      'ports': {self.lvms[0].ip: [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac1', 'ip1')],
                                self.lvms[1].ip: [
                                    l2pop_rpc.PortInfo('mac2', 'ip2')],
                                '192.0.2.1': [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac3', 'ip3')]}}}
        with mock.patch.object(self.agent,
                               'setup_tunnel_port') as setup_tun_fn:
            self.agent.fdb_add(None, fdb_entry)
            calls = [
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip1', 'mac1'),
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip2', 'mac2')
            ]
            self.ryuapp.add_arp_table_entry.assert_has_calls(calls,
                                                             any_order=True)
            self.assertFalse(setup_tun_fn.called)

    def test_add_arp_table_entry_vlan(self):
        self._test_add_arp_table_entry_non_tunnel('vlan')

    def test_add_arp_table_entry_flat(self):
        self._test_add_arp_table_entry_non_tunnel('flat')

    def test_add_arp_table_entry_local(self):
        self._test_add_arp_table_entry_non_tunnel('local')

    def test_del_arp_table_entry(self):
        self._prepare_l2_pop_ofports()
        fdb_entry = {self.lvms[0].net:
                     {'network_type': self.tunnel_type,
                      'segment_id': 'tun1',
                      'ports': {self.lvms[0].ip: [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac1', 'ip1')],
                                self.lvms[1].ip: [
                                    l2pop_rpc.PortInfo('mac2', 'ip2')],
                                '192.0.2.1': [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac3', 'ip3')]}}}
        with mock.patch.object(self.agent,
                               'cleanup_tunnel_port') as cleanup_tun_fn:
            self.agent.fdb_remove(None, fdb_entry)
            calls = [
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip1'),
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip2')
            ]
            self.ryuapp.del_arp_table_entry.assert_has_calls(calls,
                                                             any_order=True)
            self.assertEqual(2, cleanup_tun_fn.call_count)

    def _test_del_arp_table_entry_non_tunnel(self, network_type):
        self._prepare_l2_pop_ofports(network_type=network_type)
        fdb_entry = {self.lvms[0].net:
                     {'network_type': network_type,
                      'segment_id': 'tun1',
                      'ports': {self.lvms[0].ip: [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac1', 'ip1')],
                                self.lvms[1].ip: [
                                    l2pop_rpc.PortInfo('mac2', 'ip2')],
                                '192.0.2.1': [
                                    FLOODING_ENTRY,
                                    l2pop_rpc.PortInfo('mac3', 'ip3')]}}}
        with mock.patch.object(self.agent,
                               'cleanup_tunnel_port') as cleanup_tun_fn:
            self.agent.fdb_remove(None, fdb_entry)
            calls = [
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip1'),
                mock.call(self.agent.local_vlan_map[self.lvms[0].net].vlan,
                          'ip2')
            ]
            self.ryuapp.del_arp_table_entry.assert_has_calls(calls,
                                                             any_order=True)
            self.assertFalse(cleanup_tun_fn.called)

    def test_del_arp_table_entry_vlan(self):
        self._test_del_arp_table_entry_non_tunnel('vlan')

    def test_del_arp_table_entry_flat(self):
        self._test_del_arp_table_entry_non_tunnel('flat')

    def test_del_arp_table_entry_local(self):
        self._test_del_arp_table_entry_non_tunnel('local')

    def test_recl_lv_port_to_preserve(self):
        self._prepare_l2_pop_ofports()
        self.agent.enable_tunneling = True
        with mock.patch.object(
            self.agent.int_br, 'delete_port'
        ) as del_port_fn:
            self.agent.reclaim_local_vlan(self.lvms[0].net)
            self.assertFalse(del_port_fn.called)

    def test__setup_tunnel_port_error_negative(self):
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'add_tunnel_port',
                              return_value=ovs_lib.INVALID_OFPORT),
            mock.patch.object(self.mod_agent.LOG, 'error')
        ) as (add_tunnel_port_fn, log_error_fn):
            ofport = self.agent._setup_tunnel_port(
                self.agent.int_br, 'gre-1', p_const.TYPE_GRE)
            add_tunnel_port_fn.assert_called_once_with(
                'gre-1', 'flow', '0', p_const.TYPE_GRE,
                self.agent.vxlan_udp_port, self.agent.dont_fragment)
            log_error_fn.assert_called_once_with(
                _("Failed to set-up %(type)s tunnel port"),
                {'type': p_const.TYPE_GRE})
            self.assertEqual(ofport, 0)

    def test_setup_tunnel_port_returns_zero_for_failed_port_add(self):
        with mock.patch.object(self.agent.int_br, 'add_tunnel_port',
                               return_value=ovs_lib.INVALID_OFPORT):
            result = self.agent._setup_tunnel_port(self.agent.int_br, 'gre-1',
                                                   p_const.TYPE_GRE)
        self.assertEqual(0, result)

    def test_tunnel_sync(self):
        self.agent.local_ip = 'agent_ip'
        self.agent.context = 'fake_context'
        self.agent.tunnel_types = ['vxlan']
        self.agent.host = cfg.CONF.host
        with mock.patch.object(
            self.agent.plugin_rpc, 'tunnel_sync'
        ) as tunnel_sync_rpc_fn:
            self.agent.tunnel_sync()
            tunnel_sync_rpc_fn.assert_called_once_with(
                self.agent.context,
                self.agent.local_ip,
                self.agent.tunnel_types[0],
                self.agent.host)

    def test__get_ports(self):
        ofpp = importutils.import_module('ryu.ofproto.ofproto_v1_3_parser')
        reply = [ofpp.OFPPortDescStatsReply(body=[ofpp.OFPPort(name='hoge',
                                                               port_no=8)])]
        sendmsg = mock.Mock(return_value=reply)
        self.mod_agent.ryu_api.send_msg = sendmsg
        result = self.agent._get_ports(self.agent.int_br)
        result = list(result)  # convert generator to list.
        self.assertEqual(1, len(result))
        self.assertEqual('hoge', result[0].port_name)
        self.assertEqual(8, result[0].ofport)
        expected_msg = ofpp.OFPPortDescStatsRequest(
            datapath=self.agent.int_br.datapath)
        sendmsg.assert_has_calls([mock.call(app=self.agent.ryuapp,
            msg=expected_msg, reply_cls=ofpp.OFPPortDescStatsReply,
            reply_multi=True)])

    def test__get_ofport_names(self):
        names = ['p111', 'p222', 'p333']
        ps = [_mock_port(True, x) for x in names]
        with mock.patch.object(self.agent, '_get_ports',
                               return_value=ps) as _get_ports:
            result = self.agent._get_ofport_names('hoge')
        _get_ports.assert_called_once_with('hoge')
        self.assertEqual(set(names), result)

    def test_port_dead(self):
        net = "539b161f-b31a-11e4-8c19-08606e7f74e7"
        mac = "08:60:6e:7f:74:e7"
        ofport = 111
        vlan = 99
        port = mock.Mock()
        port.ofport = ofport
        port.vif_mac = mac
        lvm = mock.Mock()
        lvm.vlan = vlan
        self.agent.local_vlan_map[net] = lvm
        with contextlib.nested(
            mock.patch.object(self.agent.int_br, 'check_in_port_delete_port'),
            mock.patch.object(self.agent.int_br, 'local_out_delete_port'),
        ) as (check_in_port_delete_port, local_out_delete_port):
            self.agent.port_dead(port, net_uuid=net)
        check_in_port_delete_port.assert_called_once_with(ofport)
        local_out_delete_port.assert_called_once_with(vlan, mac)
