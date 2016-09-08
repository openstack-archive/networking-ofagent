# Copyright (C) 2014-2015 VA Linux Systems Japan K.K.
# Copyright (C) 2014-2015 Fumihiko Kakuma <kakuma at valinux co jp>
# Copyright (C) 2014 YAMAMOTO Takashi <yamamoto at valinux co jp>
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

import sys

from oslo_config import cfg
from ryu.base import app_manager
from ryu import cfg as ryu_cfg

from neutron.common import config as common_config


def main():
    common_config.init(sys.argv[1:])
    # the following check is a transitional workaround to make this work
    # with different versions of ryu.
    # TODO(yamamoto) remove this later
    if ryu_cfg.CONF is not cfg.CONF:
        ryu_cfg.CONF(project='ryu', args=[])
    common_config.setup_logging()
    app_manager.AppManager.run_apps([
        'networking_ofagent.plugins.ofagent.agent.ofa_neutron_agent'
    ])
