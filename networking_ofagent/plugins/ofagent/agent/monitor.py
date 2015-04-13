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

import collections

from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from neutron.i18n import _LI

from networking_ofagent.plugins.ofagent.agent import ports

LOG = logging.getLogger(__name__)

PortStatus = collections.namedtuple('PortStatus', 'reason port name')


class PortMonitor(object):

    def __init__(self):
        self.port_status_list = []

    @log_helpers.log_method_call
    def port_status_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofp = datapath.ofproto
        port_no = msg.desc.port_no
        if msg.reason == ofp.OFPPR_DELETE:
            reason = 'del'
        else:
            LOG.info(_LI("Received illeagal reason msg: %s"), msg)
            return
        port = ports.Port.from_ofp_port(msg.desc)
        if port.is_neutron_port():
            ps = PortStatus(reason=reason, port=port,
                            name=port.normalized_port_name())
            LOG.debug("port status reason: %(reason)s status name: %(name)s "
                      "port no: %(port_no)s port: %(port)s",
                      {'reason': ps.reason, 'name': ps.name,
                       'port_no': port_no, 'port': ps.port})
            self.port_status_list.append(ps)

    def get_port_status_list(self):
        port_status_list = self.port_status_list
        self.port_status_list = []
        LOG.debug("get_port_status_list port_status_list: %s",
                  port_status_list)
        return port_status_list
