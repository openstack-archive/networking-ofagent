Release Procedure
=================

1. Create a signed tag for the new release and push it to gerrit
   [#tagging_a_release]_ .
   Infra machinary will take care of the rest and automatically upload
   the release to PyPI.

2. Bump the version in setup.cfg.  This step effectively opens
   the development for the next release.

3. Tweak requirements.txt [#neutron_requirements]_ in neutron tree
   if necessary.

.. [#tagging_a_release] http://docs.openstack.org/infra/manual/creators.html#tagging-a-release

.. [#neutron_requirements] http://git.openstack.org/cgit/openstack/neutron/tree/neutron/plugins/ml2/drivers/ofagent/requirements.txt
