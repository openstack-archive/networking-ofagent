========================
DevStack external plugin
========================

This directory contains files used by DevStack's external plugins
mechanism [#external_plugins]_.

local.conf recipe to use this::

    enable_plugin networking-ofagent https://git.openstack.org/openstack/networking-ofagent
    Q_PLUGIN=ml2
    Q_AGENT=ofagent
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=ofagent,l2population

.. [#external_plugins] Externally Hosted Plugins
   http://docs.openstack.org/developer/devstack/plugins.html#externally-hosted-plugins#
