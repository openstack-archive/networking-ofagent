========================
DevStack external plugin
========================

This directory contains files used by DevStack's external plugins
mechanism [1].

local.conf recipe to use this:

    enable_plugin networking-ofagent https://git.openstack.org/stackforge/networking-ofagent
    Q_PLUGIN=ml2
    Q_AGENT=ofagent_agent
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=ofagent,l2population

[1] http://docs.openstack.org/developer/devstack/plugins.html#externally-hosted-plugins#
