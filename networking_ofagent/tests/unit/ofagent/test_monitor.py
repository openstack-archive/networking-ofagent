# Copyright (C) 2015 VA Linux Systems Japan K.K.
# Copyright (C) 2015 Fumihiko Kakuma <kakuma at valinux co jp>
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

import mock

from oslo_utils import importutils

from networking_ofagent.plugins.ofagent.agent import ports
from networking_ofagent.tests.unit.ofagent import ofa_test_base


_PORTMONITOR_NAME = 'networking_ofagent.plugins.ofagent.agent.monitor'


class TestPortMonitor(ofa_test_base.OFATestBase):

    def setUp(self):
        super(TestPortMonitor, self).setUp()

        self.mod = importutils.import_module(_PORTMONITOR_NAME)
        self.portmonitor = self.mod.PortMonitor()
        self.ev = mock.Mock()
        self.datapath = self._mk_test_dp('dp')
        self.msg = mock.Mock()
        self.msg.datapath = self.datapath
        self.ev.msg = self.msg

    def _test_port_status_handler(self, reason, name, pno, check_lng=None):
        msg = self.ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        ofpp = dp.ofproto_parser
        msg.desc = ofpp.OFPPort(name=name, port_no=pno)
        if reason == 'del':
            msg.reason = ofp.OFPPR_DELETE
        else:
            msg.reason = reason
        is_neutron = False
        if ports._is_neutron_port(name):
            is_neutron = True
            expected_name = ports._normalize_port_name(name)
        #expected_name = n_const.TAP_DEVICE_PREFIX + name[3:]
        lng = len(self.portmonitor.port_status_list)
        self.portmonitor.port_status_handler(self.ev)
        if is_neutron:
            if reason == 'del':
                ps = self.portmonitor.port_status_list[-1]
                self.assertEqual(len(self.portmonitor.port_status_list),
                                 lng + 1)
                self.assertEqual(ps.name, expected_name)
                self.assertEqual(ps.reason, reason)
                self.assertEqual(ps.port.port_name, name)
                self.assertEqual(ps.port.ofport, pno)
                if check_lng:
                    self.assertEqual(len(self.portmonitor.port_status_list),
                                     check_lng)
            else:
                self.assertEqual(len(self.portmonitor.port_status_list), lng)
        else:
            self.assertEqual(len(self.portmonitor.port_status_list), lng)

    def test_port_status_handler_reason_del(self):
        self._test_port_status_handler('del', 'qr-328827e7-2d', 4)

    def test_port_status_handler_multiple_reason(self):
        self._test_port_status_handler('add', 'qr-328827e7-2d', 4)
        self._test_port_status_handler('del', 'qr-328827e7-3d', 5)
        self._test_port_status_handler('add', 'qr-328827e7-4d', 6, 3)

    def test_port_status_handler_ignore_illegal_reason(self):
        self._test_port_status_handler('test', 'qvo328827e7-2d', 6)

    def test_port_status_handler_ignore_non_neutron_port(self):
        self._test_port_status_handler('test', 'xxx328827e7-2d', 7)

    def test_get_port_status_list(self):
        self._test_port_status_handler('add', 'qr-328827e7-2d', 4)
        self._test_port_status_handler('del', 'qr-328827e7-3d', 5)
        self._test_port_status_handler('add', 'qr-328827e7-4d', 6)
        expected = self.portmonitor.port_status_list
        portstatus = self.portmonitor.get_port_status_list()
        self.assertEqual(portstatus, expected)
        self.assertFalse(self.portmonitor.port_status_list)
