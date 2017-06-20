# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

import datetime

import mock
from oslo_db.sqlalchemy import enginefacade
from oslo_utils import timeutils
from six.moves import range

from nova import compute
from nova.compute import flavors
import nova.conf
from nova import context
from nova import db
from nova.db.sqlalchemy import api as sqa_api
from nova.db.sqlalchemy import models as sqa_models
from nova import exception
from nova import quota
from nova import test
import nova.tests.unit.image.fake

CONF = nova.conf.CONF


def _get_fake_get_usages(updates=None):
    # These values are not realistic (they should all be 0) and are
    # only for testing that countable usages get included in the
    # results.
    usages = {'security_group_rules': {'in_use': 1},
              'key_pairs': {'in_use': 2},
              'server_group_members': {'in_use': 3}}
    if updates:
        usages.update(updates)

    def fake_get_usages(*a, **k):
        return usages

    return fake_get_usages


class QuotaIntegrationTestCase(test.TestCase):

    REQUIRES_LOCKING = True

    def setUp(self):
        super(QuotaIntegrationTestCase, self).setUp()
        self.flags(instances=2,
                   cores=4,
                   group='quota')

        self.user_id = 'admin'
        self.project_id = 'admin'
        self.context = context.RequestContext(self.user_id,
                                              self.project_id,
                                              is_admin=True)

        nova.tests.unit.image.fake.stub_out_image_service(self)

        self.compute_api = compute.API()

        def fake_validate_networks(context, requested_networks, num_instances):
            return num_instances

        # we aren't testing network quota in these tests when creating a server
        # so just mock that out and assume network (port) quota is OK
        self.compute_api.network_api.validate_networks = (
            mock.Mock(side_effect=fake_validate_networks))

    def tearDown(self):
        super(QuotaIntegrationTestCase, self).tearDown()
        nova.tests.unit.image.fake.FakeImageService_reset()

    def _create_instance(self, cores=2):
        """Create a test instance."""
        inst = {}
        inst['image_id'] = 'cedef40a-ed67-4d10-800e-17455edce175'
        inst['reservation_id'] = 'r-fakeres'
        inst['user_id'] = self.user_id
        inst['project_id'] = self.project_id
        inst['instance_type_id'] = '3'  # m1.large
        inst['vcpus'] = cores
        return db.instance_create(self.context, inst)

    def test_too_many_instances(self):
        instance_uuids = []
        for i in range(CONF.quota.instances):
            instance = self._create_instance()
            instance_uuids.append(instance['uuid'])
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        try:
            self.compute_api.create(self.context, min_count=1, max_count=1,
                                    instance_type=inst_type,
                                    image_href=image_uuid)
        except exception.QuotaError as e:
            expected_kwargs = {'code': 413,
                               'req': '1, 1',
                               'used': '4, 2',
                               'allowed': '4, 2',
                               'overs': 'cores, instances'}
            self.assertEqual(expected_kwargs, e.kwargs)
        else:
            self.fail('Expected QuotaError exception')
        for instance_uuid in instance_uuids:
            db.instance_destroy(self.context, instance_uuid)

    def test_too_many_cores(self):
        instance = self._create_instance(cores=4)
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        try:
            self.compute_api.create(self.context, min_count=1, max_count=1,
                                    instance_type=inst_type,
                                    image_href=image_uuid)
        except exception.QuotaError as e:
            expected_kwargs = {'code': 413,
                               'req': '1',
                               'used': '4',
                               'allowed': '4',
                               'overs': 'cores'}
            self.assertEqual(expected_kwargs, e.kwargs)
        else:
            self.fail('Expected QuotaError exception')
        db.instance_destroy(self.context, instance['uuid'])

    def test_many_cores_with_unlimited_quota(self):
        # Setting cores quota to unlimited:
        self.flags(cores=-1, group='quota')
        instance = self._create_instance(cores=4)
        db.instance_destroy(self.context, instance['uuid'])

    def test_too_many_addresses(self):
        # This test is specifically relying on nova-network.
        self.flags(use_neutron=False,
                   network_manager='nova.network.manager.FlatDHCPManager')
        self.flags(floating_ips=1, group='quota')
        # Apparently needed by the RPC tests...
        self.network = self.start_service('network',
                                          manager=CONF.network_manager)
        address = '192.168.0.100'
        db.floating_ip_create(context.get_admin_context(),
                              {'address': address,
                               'pool': 'nova',
                               'project_id': self.project_id})
        self.assertRaises(exception.QuotaError,
                          self.network.allocate_floating_ip,
                          self.context,
                          self.project_id)
        db.floating_ip_destroy(context.get_admin_context(), address)

    def test_auto_assigned(self):
        # This test is specifically relying on nova-network.
        self.flags(use_neutron=False,
                   network_manager='nova.network.manager.FlatDHCPManager')
        self.flags(floating_ips=1, group='quota')
        # Apparently needed by the RPC tests...
        self.network = self.start_service('network',
                                          manager=CONF.network_manager)
        address = '192.168.0.100'
        db.floating_ip_create(context.get_admin_context(),
                              {'address': address,
                               'pool': 'nova',
                               'project_id': self.project_id})
        # auto allocated addresses should not be counted
        self.assertRaises(exception.NoMoreFloatingIps,
                          self.network.allocate_floating_ip,
                          self.context,
                          self.project_id,
                          True)
        db.floating_ip_destroy(context.get_admin_context(), address)

    def test_too_many_metadata_items(self):
        metadata = {}
        for i in range(CONF.quota.metadata_items + 1):
            metadata['key%s' % i] = 'value%s' % i
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        self.assertRaises(exception.QuotaError, self.compute_api.create,
                                            self.context,
                                            min_count=1,
                                            max_count=1,
                                            instance_type=inst_type,
                                            image_href=image_uuid,
                                            metadata=metadata)

    def _create_with_injected_files(self, files):
        api = self.compute_api
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        api.create(self.context, min_count=1, max_count=1,
                instance_type=inst_type, image_href=image_uuid,
                injected_files=files)

    def test_no_injected_files(self):
        api = self.compute_api
        inst_type = flavors.get_flavor_by_name('m1.small')
        image_uuid = 'cedef40a-ed67-4d10-800e-17455edce175'
        api.create(self.context,
                   instance_type=inst_type,
                   image_href=image_uuid)

    def test_max_injected_files(self):
        files = []
        for i in range(CONF.quota.injected_files):
            files.append(('/my/path%d' % i, 'config = test\n'))
        self._create_with_injected_files(files)  # no QuotaError

    def test_too_many_injected_files(self):
        files = []
        for i in range(CONF.quota.injected_files + 1):
            files.append(('/my/path%d' % i, 'my\ncontent%d\n' % i))
        self.assertRaises(exception.QuotaError,
                          self._create_with_injected_files, files)

    def test_max_injected_file_content_bytes(self):
        max = CONF.quota.injected_file_content_bytes
        content = ''.join(['a' for i in range(max)])
        files = [('/test/path', content)]
        self._create_with_injected_files(files)  # no QuotaError

    def test_too_many_injected_file_content_bytes(self):
        max = CONF.quota.injected_file_content_bytes
        content = ''.join(['a' for i in range(max + 1)])
        files = [('/test/path', content)]
        self.assertRaises(exception.QuotaError,
                          self._create_with_injected_files, files)

    def test_max_injected_file_path_bytes(self):
        max = CONF.quota.injected_file_path_length
        path = ''.join(['a' for i in range(max)])
        files = [(path, 'config = quotatest')]
        self._create_with_injected_files(files)  # no QuotaError

    def test_too_many_injected_file_path_bytes(self):
        max = CONF.quota.injected_file_path_length
        path = ''.join(['a' for i in range(max + 1)])
        files = [(path, 'config = quotatest')]
        self.assertRaises(exception.QuotaError,
                          self._create_with_injected_files, files)

    def test_reservation_expire(self):
        self.useFixture(test.TimeOverride())

        def assertInstancesReserved(reserved):
            result = quota.QUOTAS.get_project_quotas(self.context,
                                                     self.context.project_id)
            self.assertEqual(result['instances']['reserved'], reserved)

        quota.QUOTAS.reserve(self.context,
                             expire=60,
                             instances=2)

        assertInstancesReserved(2)

        timeutils.advance_time_seconds(80)

        quota.QUOTAS.expire(self.context)

        assertInstancesReserved(0)


@enginefacade.transaction_context_provider
class FakeContext(context.RequestContext):
    def __init__(self, project_id, quota_class):
        super(FakeContext, self).__init__(project_id=project_id,
                                          quota_class=quota_class)
        self.is_admin = False
        self.user_id = 'fake_user'
        self.project_id = project_id
        self.quota_class = quota_class
        self.read_deleted = 'no'

    def elevated(self):
        elevated = self.__class__(self.project_id, self.quota_class)
        elevated.is_admin = True
        return elevated


class FakeDriver(object):
    def __init__(self, by_project=None, by_user=None, by_class=None,
                 reservations=None):
        self.called = []
        self.by_project = by_project or {}
        self.by_user = by_user or {}
        self.by_class = by_class or {}
        self.reservations = reservations or []

    def get_by_project_and_user(self, context, project_id, user_id, resource):
        self.called.append(('get_by_project_and_user',
                            context, project_id, user_id, resource))
        try:
            return self.by_user[user_id][resource]
        except KeyError:
            raise exception.ProjectUserQuotaNotFound(project_id=project_id,
                                                     user_id=user_id)

    def get_by_project(self, context, project_id, resource):
        self.called.append(('get_by_project', context, project_id, resource))
        try:
            return self.by_project[project_id][resource]
        except KeyError:
            raise exception.ProjectQuotaNotFound(project_id=project_id)

    def get_by_class(self, context, quota_class, resource):
        self.called.append(('get_by_class', context, quota_class, resource))
        try:
            return self.by_class[quota_class][resource]
        except KeyError:
            raise exception.QuotaClassNotFound(class_name=quota_class)

    def get_defaults(self, context, resources):
        self.called.append(('get_defaults', context, resources))
        return resources

    def get_class_quotas(self, context, resources, quota_class,
                         defaults=True):
        self.called.append(('get_class_quotas', context, resources,
                            quota_class, defaults))
        return resources

    def get_user_quotas(self, context, resources, project_id, user_id,
                        quota_class=None, defaults=True, usages=True):
        self.called.append(('get_user_quotas', context, resources,
                            project_id, user_id, quota_class, defaults,
                            usages))
        return resources

    def get_project_quotas(self, context, resources, project_id,
                           quota_class=None, defaults=True, usages=True,
                           remains=False):
        self.called.append(('get_project_quotas', context, resources,
                            project_id, quota_class, defaults, usages,
                            remains))
        return resources

    def limit_check(self, context, resources, values, project_id=None,
                    user_id=None):
        self.called.append(('limit_check', context, resources,
                            values, project_id, user_id))

    def limit_check_project_and_user(self, context, resources,
                                     project_values=None, user_values=None,
                                     project_id=None, user_id=None):
        self.called.append(('limit_check_project_and_user', context, resources,
                            project_values, user_values, project_id, user_id))

    def reserve(self, context, resources, deltas, expire=None,
                project_id=None, user_id=None):
        self.called.append(('reserve', context, resources, deltas,
                            expire, project_id, user_id))
        return self.reservations

    def commit(self, context, reservations, project_id=None, user_id=None):
        self.called.append(('commit', context, reservations, project_id,
                            user_id))

    def rollback(self, context, reservations, project_id=None, user_id=None):
        self.called.append(('rollback', context, reservations, project_id,
                            user_id))

    def usage_reset(self, context, resources):
        self.called.append(('usage_reset', context, resources))

    def destroy_all_by_project_and_user(self, context, project_id, user_id):
        self.called.append(('destroy_all_by_project_and_user', context,
                            project_id, user_id))

    def destroy_all_by_project(self, context, project_id):
        self.called.append(('destroy_all_by_project', context, project_id))

    def expire(self, context):
        self.called.append(('expire', context))


class BaseResourceTestCase(test.TestCase):
    def test_no_flag(self):
        resource = quota.BaseResource('test_resource')

        self.assertEqual(resource.name, 'test_resource')
        self.assertIsNone(resource.flag)
        self.assertEqual(resource.default, -1)

    def test_with_flag(self):
        # We know this flag exists, so use it...
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')

        self.assertEqual(resource.name, 'test_resource')
        self.assertEqual(resource.flag, 'instances')
        self.assertEqual(resource.default, 10)

    def test_with_flag_no_quota(self):
        self.flags(instances=-1, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')

        self.assertEqual(resource.name, 'test_resource')
        self.assertEqual(resource.flag, 'instances')
        self.assertEqual(resource.default, -1)

    def test_quota_no_project_no_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver()
        context = FakeContext(None, None)
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 10)

    def test_quota_with_project_no_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=15),
                ))
        context = FakeContext('test_project', None)
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 15)

    def test_quota_no_project_with_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_class=dict(
                test_class=dict(test_resource=20),
                ))
        context = FakeContext(None, 'test_class')
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 20)

    def test_quota_with_project_with_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=15),
                ),
                            by_class=dict(
                test_class=dict(test_resource=20),
                ))
        context = FakeContext('test_project', 'test_class')
        quota_value = resource.quota(driver, context)

        self.assertEqual(quota_value, 15)

    def test_quota_override_project_with_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=15),
                override_project=dict(test_resource=20),
                ))
        context = FakeContext('test_project', 'test_class')
        quota_value = resource.quota(driver, context,
                                     project_id='override_project')

        self.assertEqual(quota_value, 20)

    def test_quota_with_project_override_class(self):
        self.flags(instances=10, group='quota')
        resource = quota.BaseResource('test_resource', 'instances')
        driver = FakeDriver(by_class=dict(
                test_class=dict(test_resource=15),
                override_class=dict(test_resource=20),
                ))
        context = FakeContext('test_project', 'test_class')
        quota_value = resource.quota(driver, context,
                                     quota_class='override_class')

        self.assertEqual(quota_value, 20)

    def test_valid_method_call_check_invalid_input(self):
        resources = {'dummy': 1}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'limit', quota.QUOTAS._resources)

    def test_valid_method_call_check_invalid_method(self):
        resources = {'key_pairs': 1}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'dummy', quota.QUOTAS._resources)

    def test_valid_method_call_check_multiple(self):
        resources = {'key_pairs': 1, 'dummy': 2}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'check', quota.QUOTAS._resources)

        resources = {'key_pairs': 1, 'instances': 2, 'dummy': 3}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'check', quota.QUOTAS._resources)

    def test_valid_method_call_check_wrong_method_reserve(self):
        resources = {'key_pairs': 1}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'reserve', quota.QUOTAS._resources)

    def test_valid_method_call_check_wrong_method_check(self):
        resources = {'instances': 1}

        self.assertRaises(exception.InvalidQuotaMethodUsage,
                          quota._valid_method_call_check_resources,
                          resources, 'check', quota.QUOTAS._resources)


class QuotaEngineTestCase(test.TestCase):
    def test_init(self):
        quota_obj = quota.QuotaEngine()

        self.assertEqual(quota_obj._resources, {})
        self.assertIsInstance(quota_obj._driver, quota.DbQuotaDriver)

    def test_init_override_string(self):
        quota_obj = quota.QuotaEngine(
            quota_driver_class='nova.tests.unit.test_quota.FakeDriver')

        self.assertEqual(quota_obj._resources, {})
        self.assertIsInstance(quota_obj._driver, FakeDriver)

    def test_init_override_obj(self):
        quota_obj = quota.QuotaEngine(quota_driver_class=FakeDriver)

        self.assertEqual(quota_obj._resources, {})
        self.assertEqual(quota_obj._driver, FakeDriver)

    def test_register_resource(self):
        quota_obj = quota.QuotaEngine()
        resource = quota.AbsoluteResource('test_resource')
        quota_obj.register_resource(resource)

        self.assertEqual(quota_obj._resources, dict(test_resource=resource))

    def test_register_resources(self):
        quota_obj = quota.QuotaEngine()
        resources = [
            quota.AbsoluteResource('test_resource1'),
            quota.AbsoluteResource('test_resource2'),
            quota.AbsoluteResource('test_resource3'),
            ]
        quota_obj.register_resources(resources)

        self.assertEqual(quota_obj._resources, dict(
                test_resource1=resources[0],
                test_resource2=resources[1],
                test_resource3=resources[2],
                ))

    def test_get_by_project_and_user(self):
        context = FakeContext('test_project', 'test_class')
        driver = FakeDriver(by_user=dict(
                fake_user=dict(test_resource=42)))
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        result = quota_obj.get_by_project_and_user(context, 'test_project',
                                       'fake_user', 'test_resource')

        self.assertEqual(driver.called, [
                ('get_by_project_and_user', context, 'test_project',
                 'fake_user', 'test_resource'),
                ])
        self.assertEqual(result, 42)

    def test_get_by_project(self):
        context = FakeContext('test_project', 'test_class')
        driver = FakeDriver(by_project=dict(
                test_project=dict(test_resource=42)))
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        result = quota_obj.get_by_project(context, 'test_project',
                                          'test_resource')

        self.assertEqual(driver.called, [
                ('get_by_project', context, 'test_project', 'test_resource'),
                ])
        self.assertEqual(result, 42)

    def test_get_by_class(self):
        context = FakeContext('test_project', 'test_class')
        driver = FakeDriver(by_class=dict(
                test_class=dict(test_resource=42)))
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        result = quota_obj.get_by_class(context, 'test_class', 'test_resource')

        self.assertEqual(driver.called, [
                ('get_by_class', context, 'test_class', 'test_resource'),
                ])
        self.assertEqual(result, 42)

    def _make_quota_obj(self, driver):
        quota_obj = quota.QuotaEngine(quota_driver_class=driver)
        resources = [
            quota.AbsoluteResource('test_resource4'),
            quota.AbsoluteResource('test_resource3'),
            quota.AbsoluteResource('test_resource2'),
            quota.AbsoluteResource('test_resource1'),
            ]
        quota_obj.register_resources(resources)

        return quota_obj

    def test_get_defaults(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result = quota_obj.get_defaults(context)

        self.assertEqual(driver.called, [
                ('get_defaults', context, quota_obj._resources),
                ])
        self.assertEqual(result, quota_obj._resources)

    def test_get_class_quotas(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.get_class_quotas(context, 'test_class')
        result2 = quota_obj.get_class_quotas(context, 'test_class', False)

        self.assertEqual(driver.called, [
                ('get_class_quotas', context, quota_obj._resources,
                 'test_class', True),
                ('get_class_quotas', context, quota_obj._resources,
                 'test_class', False),
                ])
        self.assertEqual(result1, quota_obj._resources)
        self.assertEqual(result2, quota_obj._resources)

    def test_get_user_quotas(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.get_user_quotas(context, 'test_project',
                                            'fake_user')
        result2 = quota_obj.get_user_quotas(context, 'test_project',
                                            'fake_user',
                                            quota_class='test_class',
                                            defaults=False,
                                            usages=False)

        self.assertEqual(driver.called, [
                ('get_user_quotas', context, quota_obj._resources,
                 'test_project', 'fake_user', None, True, True),
                ('get_user_quotas', context, quota_obj._resources,
                 'test_project', 'fake_user', 'test_class', False, False),
                ])
        self.assertEqual(result1, quota_obj._resources)
        self.assertEqual(result2, quota_obj._resources)

    def test_get_project_quotas(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.get_project_quotas(context, 'test_project')
        result2 = quota_obj.get_project_quotas(context, 'test_project',
                                               quota_class='test_class',
                                               defaults=False,
                                               usages=False)

        self.assertEqual(driver.called, [
                ('get_project_quotas', context, quota_obj._resources,
                 'test_project', None, True, True, False),
                ('get_project_quotas', context, quota_obj._resources,
                 'test_project', 'test_class', False, False, False),
                ])
        self.assertEqual(result1, quota_obj._resources)
        self.assertEqual(result2, quota_obj._resources)

    def test_count_as_dict_no_resource(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        self.assertRaises(exception.QuotaResourceUnknown,
                          quota_obj.count_as_dict, context, 'test_resource5',
                          True, foo='bar')

    def test_count_as_dict_wrong_resource(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        self.assertRaises(exception.QuotaResourceUnknown,
                          quota_obj.count_as_dict, context, 'test_resource1',
                          True, foo='bar')

    def test_count_as_dict(self):
        def fake_count_as_dict(context, *args, **kwargs):
            self.assertEqual(args, (True,))
            self.assertEqual(kwargs, dict(foo='bar'))
            return {'project': {'test_resource5': 5}}

        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.register_resource(
            quota.CountableResource('test_resource5', fake_count_as_dict))
        result = quota_obj.count_as_dict(context, 'test_resource5', True,
                                         foo='bar')

        self.assertEqual({'project': {'test_resource5': 5}}, result)

    def test_limit_check(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.limit_check(context, test_resource1=4, test_resource2=3,
                              test_resource3=2, test_resource4=1)

        self.assertEqual(driver.called, [
                ('limit_check', context, quota_obj._resources, dict(
                        test_resource1=4,
                        test_resource2=3,
                        test_resource3=2,
                        test_resource4=1,
                        ), None, None),
                ])

    def test_limit_check_project_and_user(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        project_values = dict(test_resource1=4, test_resource2=3)
        user_values = dict(test_resource3=2, test_resource4=1)
        quota_obj.limit_check_project_and_user(context,
                                               project_values=project_values,
                                               user_values=user_values)

        self.assertEqual([('limit_check_project_and_user', context,
                          quota_obj._resources,
                          dict(test_resource1=4, test_resource2=3),
                          dict(test_resource3=2, test_resource4=1),
                          None, None)],
                         driver.called)

    def test_reserve(self):
        context = FakeContext(None, None)
        driver = FakeDriver(reservations=[
                'resv-01', 'resv-02', 'resv-03', 'resv-04',
                ])
        quota_obj = self._make_quota_obj(driver)
        result1 = quota_obj.reserve(context, test_resource1=4,
                                    test_resource2=3, test_resource3=2,
                                    test_resource4=1)
        result2 = quota_obj.reserve(context, expire=3600,
                                    test_resource1=1, test_resource2=2,
                                    test_resource3=3, test_resource4=4)
        result3 = quota_obj.reserve(context, project_id='fake_project',
                                    test_resource1=1, test_resource2=2,
                                    test_resource3=3, test_resource4=4)

        self.assertEqual(driver.called, [
                ('reserve', context, quota_obj._resources, dict(
                        test_resource1=4,
                        test_resource2=3,
                        test_resource3=2,
                        test_resource4=1,
                        ), None, None, None),
                ('reserve', context, quota_obj._resources, dict(
                        test_resource1=1,
                        test_resource2=2,
                        test_resource3=3,
                        test_resource4=4,
                        ), 3600, None, None),
                ('reserve', context, quota_obj._resources, dict(
                        test_resource1=1,
                        test_resource2=2,
                        test_resource3=3,
                        test_resource4=4,
                        ), None, 'fake_project', None),
                ])
        self.assertEqual(result1, [
                'resv-01', 'resv-02', 'resv-03', 'resv-04',
                ])
        self.assertEqual(result2, [
                'resv-01', 'resv-02', 'resv-03', 'resv-04',
                ])
        self.assertEqual(result3, [
                'resv-01', 'resv-02', 'resv-03', 'resv-04',
                ])

    def test_commit(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.commit(context, ['resv-01', 'resv-02', 'resv-03'])

        self.assertEqual(driver.called, [
                ('commit', context, ['resv-01', 'resv-02', 'resv-03'], None,
                 None),
                ])

    def test_rollback(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.rollback(context, ['resv-01', 'resv-02', 'resv-03'])

        self.assertEqual(driver.called, [
                ('rollback', context, ['resv-01', 'resv-02', 'resv-03'], None,
                 None),
                ])

    def test_usage_reset(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.usage_reset(context, ['res1', 'res2', 'res3'])

        self.assertEqual(driver.called, [
                ('usage_reset', context, ['res1', 'res2', 'res3']),
                ])

    def test_destroy_all_by_project_and_user(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.destroy_all_by_project_and_user(context,
                                                  'test_project', 'fake_user')

        self.assertEqual(driver.called, [
                ('destroy_all_by_project_and_user', context, 'test_project',
                 'fake_user'),
                ])

    def test_destroy_all_by_project(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.destroy_all_by_project(context, 'test_project')

        self.assertEqual(driver.called, [
                ('destroy_all_by_project', context, 'test_project'),
                ])

    def test_expire(self):
        context = FakeContext(None, None)
        driver = FakeDriver()
        quota_obj = self._make_quota_obj(driver)
        quota_obj.expire(context)

        self.assertEqual(driver.called, [
                ('expire', context),
                ])

    def test_resources(self):
        quota_obj = self._make_quota_obj(None)

        self.assertEqual(quota_obj.resources,
                         ['test_resource1', 'test_resource2',
                          'test_resource3', 'test_resource4'])


class DbQuotaDriverTestCase(test.TestCase):
    def setUp(self):
        super(DbQuotaDriverTestCase, self).setUp()

        self.flags(instances=10,
                   cores=20,
                   ram=50 * 1024,
                   floating_ips=10,
                   fixed_ips=10,
                   metadata_items=128,
                   injected_files=5,
                   injected_file_content_bytes=10 * 1024,
                   injected_file_path_length=255,
                   security_groups=10,
                   security_group_rules=20,
                   server_groups=10,
                   server_group_members=10,
                   reservation_expire=86400,
                   until_refresh=0,
                   max_age=0,
                   group='quota'
                   )

        self.driver = quota.DbQuotaDriver()

        self.calls = []

        self.useFixture(test.TimeOverride())

    def test_get_defaults(self):
        # Use our pre-defined resources
        self._stub_quota_class_get_default()
        result = self.driver.get_defaults(None, quota.QUOTAS._resources)

        self.assertEqual(result, dict(
                instances=5,
                cores=20,
                ram=25 * 1024,
                floating_ips=10,
                fixed_ips=10,
                metadata_items=64,
                injected_files=5,
                injected_file_content_bytes=5 * 1024,
                injected_file_path_bytes=255,
                security_groups=10,
                security_group_rules=20,
                key_pairs=100,
                server_groups=10,
                server_group_members=10,
                ))

    def _stub_quota_class_get_default(self):
        # Stub out quota_class_get_default
        def fake_qcgd(context):
            self.calls.append('quota_class_get_default')
            return dict(
                instances=5,
                ram=25 * 1024,
                metadata_items=64,
                injected_file_content_bytes=5 * 1024,
                )
        self.stub_out('nova.db.quota_class_get_default', fake_qcgd)

    def _stub_quota_class_get_all_by_name(self):
        # Stub out quota_class_get_all_by_name
        def fake_qcgabn(context, quota_class):
            self.calls.append('quota_class_get_all_by_name')
            self.assertEqual(quota_class, 'test_class')
            return dict(
                instances=5,
                ram=25 * 1024,
                metadata_items=64,
                injected_file_content_bytes=5 * 1024,
                )
        self.stub_out('nova.db.quota_class_get_all_by_name', fake_qcgabn)

    def test_get_class_quotas(self):
        self._stub_quota_class_get_all_by_name()
        result = self.driver.get_class_quotas(None, quota.QUOTAS._resources,
                                              'test_class')

        self.assertEqual(self.calls, ['quota_class_get_all_by_name'])
        self.assertEqual(result, dict(
                instances=5,
                cores=20,
                ram=25 * 1024,
                floating_ips=10,
                fixed_ips=10,
                metadata_items=64,
                injected_files=5,
                injected_file_content_bytes=5 * 1024,
                injected_file_path_bytes=255,
                security_groups=10,
                security_group_rules=20,
                key_pairs=100,
                server_groups=10,
                server_group_members=10,
                ))

    def test_get_class_quotas_no_defaults(self):
        self._stub_quota_class_get_all_by_name()
        result = self.driver.get_class_quotas(None, quota.QUOTAS._resources,
                                              'test_class', False)

        self.assertEqual(self.calls, ['quota_class_get_all_by_name'])
        self.assertEqual(result, dict(
                instances=5,
                ram=25 * 1024,
                metadata_items=64,
                injected_file_content_bytes=5 * 1024,
                ))

    def _stub_get_by_project_and_user(self):
        def fake_qgabpau(context, project_id, user_id):
            self.calls.append('quota_get_all_by_project_and_user')
            self.assertEqual(project_id, 'test_project')
            self.assertEqual(user_id, 'fake_user')
            return dict(
                cores=10,
                injected_files=2,
                injected_file_path_bytes=127,
                )

        def fake_qgabp(context, project_id):
            self.calls.append('quota_get_all_by_project')
            self.assertEqual(project_id, 'test_project')
            return {
                'cores': 10,
                'injected_files': 2,
                'injected_file_path_bytes': 127,
                }

        def fake_qugabpau(context, project_id, user_id):
            self.calls.append('quota_usage_get_all_by_project_and_user')
            self.assertEqual(project_id, 'test_project')
            self.assertEqual(user_id, 'fake_user')
            return dict(
                instances=dict(in_use=2, reserved=2),
                cores=dict(in_use=4, reserved=4),
                ram=dict(in_use=10 * 1024, reserved=0),
                floating_ips=dict(in_use=2, reserved=0),
                metadata_items=dict(in_use=0, reserved=0),
                injected_files=dict(in_use=0, reserved=0),
                injected_file_content_bytes=dict(in_use=0, reserved=0),
                injected_file_path_bytes=dict(in_use=0, reserved=0),
                )

        self.stub_out('nova.db.quota_get_all_by_project_and_user',
                       fake_qgabpau)
        self.stub_out('nova.db.quota_get_all_by_project', fake_qgabp)
        self.stub_out('nova.db.quota_usage_get_all_by_project_and_user',
                       fake_qugabpau)

        self._stub_quota_class_get_all_by_name()

    def _get_fake_countable_resources(self):
        # Create several countable resources with fake count functions
        def fake_instances_cores_ram_count(*a, **k):
            return {'project': {'instances': 2, 'cores': 4, 'ram': 1024},
                    'user': {'instances': 1, 'cores': 2, 'ram': 512}}

        def fake_security_group_count(*a, **k):
            return {'project': {'security_groups': 2},
                    'user': {'security_groups': 1}}

        def fake_server_group_count(*a, **k):
            return {'project': {'server_groups': 5},
                    'user': {'server_groups': 3}}

        resources = {}
        resources['key_pairs'] = quota.CountableResource(
            'key_pairs', lambda *a, **k: {'user': {'key_pairs': 1}},
            'key_pairs')
        resources['instances'] = quota.CountableResource(
            'instances', fake_instances_cores_ram_count, 'instances')
        resources['cores'] = quota.CountableResource(
            'cores', fake_instances_cores_ram_count, 'cores')
        resources['ram'] = quota.CountableResource(
            'ram', fake_instances_cores_ram_count, 'ram')
        resources['security_groups'] = quota.CountableResource(
            'security_groups', fake_security_group_count, 'security_groups')
        resources['floating_ips'] = quota.CountableResource(
            'floating_ips', lambda *a, **k: {'project': {'floating_ips': 4}},
            'floating_ips')
        resources['fixed_ips'] = quota.CountableResource(
            'fixed_ips', lambda *a, **k: {'project': {'fixed_ips': 5}},
            'fixed_ips')
        resources['server_groups'] = quota.CountableResource(
            'server_groups', fake_server_group_count, 'server_groups')
        resources['server_group_members'] = quota.CountableResource(
            'server_group_members',
            lambda *a, **k: {'user': {'server_group_members': 7}},
            'server_group_members')
        resources['security_group_rules'] = quota.CountableResource(
            'security_group_rules',
            lambda *a, **k: {'project': {'security_group_rules': 8}},
            'security_group_rules')
        return resources

    def test_get_usages_for_project(self):
        resources = self._get_fake_countable_resources()
        actual = self.driver._get_usages(
            FakeContext('test_project', 'test_class'), resources,
            'test_project')
        # key_pairs, server_group_members, and security_group_rules are never
        # counted as a usage. Their counts are only for quota limit checking.
        expected = {'key_pairs': {'in_use': 0},
                    'instances': {'in_use': 2},
                    'cores': {'in_use': 4},
                    'ram': {'in_use': 1024},
                    'security_groups': {'in_use': 2},
                    'floating_ips': {'in_use': 4},
                    'fixed_ips': {'in_use': 5},
                    'server_groups': {'in_use': 5},
                    'server_group_members': {'in_use': 0},
                    'security_group_rules': {'in_use': 0}}
        self.assertEqual(expected, actual)

    def test_get_usages_for_user(self):
        resources = self._get_fake_countable_resources()
        actual = self.driver._get_usages(
            FakeContext('test_project', 'test_class'), resources,
            'test_project', user_id='fake_user')
        # key_pairs, server_group_members, and security_group_rules are never
        # counted as a usage. Their counts are only for quota limit checking.
        expected = {'key_pairs': {'in_use': 0},
                    'instances': {'in_use': 1},
                    'cores': {'in_use': 2},
                    'ram': {'in_use': 512},
                    'security_groups': {'in_use': 1},
                    'floating_ips': {'in_use': 4},
                    'fixed_ips': {'in_use': 5},
                    'server_groups': {'in_use': 3},
                    'server_group_members': {'in_use': 0},
                    'security_group_rules': {'in_use': 0}}
        self.assertEqual(expected, actual)

    @mock.patch('nova.quota.DbQuotaDriver._get_usages')
    def test_get_user_quotas(self, mock_get_usages):
        # This will test that the counted usage will not be overwritten by
        # the quota_usages records (in_use=2, reserved=2) from the database.
        usages = {'instances': {'in_use': 5}}
        mock_get_usages.side_effect = _get_fake_get_usages(updates=usages)

        self.maxDiff = None
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project_and_user',
                'quota_class_get_all_by_name',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=5,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
               floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    def _stub_get_by_project_and_user_specific(self):
        def fake_quota_get(context, project_id, resource, user_id=None):
            self.calls.append('quota_get')
            self.assertEqual(project_id, 'test_project')
            self.assertEqual(user_id, 'fake_user')
            self.assertEqual(resource, 'test_resource')
            return dict(
                test_resource=dict(in_use=20, reserved=10),
                )
        self.stub_out('nova.db.quota_get', fake_quota_get)

    def test_get_by_project_and_user(self):
        self._stub_get_by_project_and_user_specific()
        result = self.driver.get_by_project_and_user(
            FakeContext('test_project', 'test_class'),
            'test_project', 'fake_user', 'test_resource')

        self.assertEqual(self.calls, ['quota_get'])
        self.assertEqual(result, dict(
            test_resource=dict(in_use=20, reserved=10),
            ))

    def _stub_get_by_project(self):
        def fake_qgabp(context, project_id):
            self.calls.append('quota_get_all_by_project')
            self.assertEqual(project_id, 'test_project')
            return dict(
                cores=10,
                injected_files=2,
                injected_file_path_bytes=127,
                )

        def fake_qugabp(context, project_id):
            self.calls.append('quota_usage_get_all_by_project')
            self.assertEqual(project_id, 'test_project')
            return dict(
                instances=dict(in_use=2, reserved=2),
                cores=dict(in_use=4, reserved=4),
                ram=dict(in_use=10 * 1024, reserved=0),
                floating_ips=dict(in_use=2, reserved=0),
                metadata_items=dict(in_use=0, reserved=0),
                injected_files=dict(in_use=0, reserved=0),
                injected_file_content_bytes=dict(in_use=0, reserved=0),
                injected_file_path_bytes=dict(in_use=0, reserved=0),
                )

        def fake_quota_get_all(context, project_id):
            self.calls.append('quota_get_all')
            self.assertEqual(project_id, 'test_project')
            return [sqa_models.ProjectUserQuota(resource='instances',
                                                hard_limit=5),
                    sqa_models.ProjectUserQuota(resource='cores',
                                                hard_limit=2)]

        self.stub_out('nova.db.quota_get_all_by_project', fake_qgabp)
        self.stub_out('nova.db.quota_usage_get_all_by_project', fake_qugabp)
        self.stub_out('nova.db.quota_get_all', fake_quota_get_all)

        self._stub_quota_class_get_all_by_name()
        self._stub_quota_class_get_default()

    @mock.patch('nova.quota.DbQuotaDriver._get_usages')
    def test_get_project_quotas(self, mock_get_usages):
        # This will test that the counted usage will not be overwritten by
        # the quota_usages records (in_use=2, reserved=2) from the database.
        usages = {'instances': {'in_use': 5}}
        mock_get_usages.side_effect = _get_fake_get_usages(updates=usages)

        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=5,
                    reserved=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
               floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_with_remains(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', remains=True)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                'quota_get_all',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=2,
                    remains=0,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    remains=8,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    remains=25 * 1024,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    remains=10,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    remains=10,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    remains=64,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    remains=2,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    remains=5 * 1024,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    remains=127,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    remains=10,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    remains=20,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    remains=100,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    remains=10,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    remains=10,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas_alt_context_no_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('other_project', None)
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project_and_user',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=10,
                    in_use=2,
                    reserved=2,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
                ram=dict(
                    limit=50 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=128,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=10 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_alt_context_no_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('other_project', None)
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=2,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
               floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas_alt_context_with_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('other_project', 'other_class')
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user',
            quota_class='test_class')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project_and_user',
                'quota_class_get_all_by_name',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=2,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_alt_context_with_class(self, mock_get_usages):
        self.maxDiff = None
        self._stub_get_by_project()
        ctxt = FakeContext('other_project', 'other_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project',
            quota_class='test_class')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    in_use=2,
                    reserved=2,
                    ),
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    in_use=10 * 1024,
                    reserved=0,
                    ),
                floating_ips=dict(
                    limit=10,
                    in_use=2,
                    reserved=0,
                    ),
                fixed_ips=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                metadata_items=dict(
                    limit=64,
                    in_use=0,
                    reserved=0,
                    ),
                injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                security_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                security_group_rules=dict(
                    limit=20,
                    in_use=1,
                    reserved=0,
                    ),
                key_pairs=dict(
                    limit=100,
                    in_use=2,
                    reserved=0,
                    ),
                server_groups=dict(
                    limit=10,
                    in_use=0,
                    reserved=0,
                    ),
                server_group_members=dict(
                    limit=10,
                    in_use=3,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_user_quotas_no_defaults(self, mock_get_usages):
        self._stub_get_by_project_and_user()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_user_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', 'fake_user',
            defaults=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project_and_user',
                'quota_class_get_all_by_name',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project',
                                                user_id='fake_user')
        self.assertEqual(result, dict(
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
               injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                ))

    @mock.patch('nova.quota.DbQuotaDriver._get_usages',
                side_effect=_get_fake_get_usages())
    def test_get_project_quotas_no_defaults(self, mock_get_usages):
        self._stub_get_by_project()
        ctxt = FakeContext('test_project', 'test_class')
        result = self.driver.get_project_quotas(
            ctxt, quota.QUOTAS._resources, 'test_project', defaults=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_usage_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        mock_get_usages.assert_called_once_with(ctxt, quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(result, dict(
                cores=dict(
                    limit=10,
                    in_use=4,
                    reserved=4,
                    ),
               injected_files=dict(
                    limit=2,
                    in_use=0,
                    reserved=0,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    in_use=0,
                    reserved=0,
                    ),
                ))

    def test_get_user_quotas_no_usages(self):
        self._stub_get_by_project_and_user()
        result = self.driver.get_user_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', 'fake_user', usages=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project_and_user',
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                ])
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    ),
                cores=dict(
                    limit=10,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    ),
                floating_ips=dict(
                    limit=10,
                    ),
                fixed_ips=dict(
                    limit=10,
                    ),
                metadata_items=dict(
                    limit=64,
                    ),
                injected_files=dict(
                    limit=2,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    ),
                security_groups=dict(
                    limit=10,
                    ),
                security_group_rules=dict(
                    limit=20,
                    ),
                key_pairs=dict(
                    limit=100,
                    ),
                server_groups=dict(
                    limit=10,
                    ),
                server_group_members=dict(
                    limit=10,
                    ),
                ))

    def test_get_project_quotas_no_usages(self):
        self._stub_get_by_project()
        result = self.driver.get_project_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', usages=False)

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'quota_class_get_all_by_name',
                'quota_class_get_default',
                ])
        self.assertEqual(result, dict(
                instances=dict(
                    limit=5,
                    ),
                cores=dict(
                    limit=10,
                    ),
                ram=dict(
                    limit=25 * 1024,
                    ),
                floating_ips=dict(
                    limit=10,
                    ),
                fixed_ips=dict(
                    limit=10,
                    ),
                metadata_items=dict(
                    limit=64,
                    ),
                injected_files=dict(
                    limit=2,
                    ),
                injected_file_content_bytes=dict(
                    limit=5 * 1024,
                    ),
                injected_file_path_bytes=dict(
                    limit=127,
                    ),
                security_groups=dict(
                    limit=10,
                    ),
                security_group_rules=dict(
                    limit=20,
                    ),
                key_pairs=dict(
                    limit=100,
                    ),
                server_groups=dict(
                    limit=10,
                    ),
                server_group_members=dict(
                    limit=10,
                    ),
                ))

    def _stub_get_settable_quotas(self):

        def fake_quota_get_all_by_project(context, project_id):
            self.calls.append('quota_get_all_by_project')
            return {'floating_ips': 20}

        def fake_get_project_quotas(dbdrv, context, resources, project_id,
                                    quota_class=None, defaults=True,
                                    usages=True, remains=False,
                                    project_quotas=None):
            self.calls.append('get_project_quotas')
            result = {}
            for k, v in resources.items():
                limit = v.default
                reserved = 0
                if k == 'instances':
                    remains = v.default - 5
                    in_use = 1
                elif k == 'cores':
                    remains = -1
                    in_use = 5
                    limit = -1
                elif k == 'floating_ips':
                    remains = 20
                    in_use = 0
                    limit = 20
                else:
                    remains = v.default
                    in_use = 0
                result[k] = {'limit': limit, 'in_use': in_use,
                             'reserved': reserved, 'remains': remains}
            return result

        def fake_process_quotas_in_get_user_quotas(dbdrv, context, resources,
                                                   project_id, quotas,
                                                   quota_class=None,
                                                   defaults=True, usages=None,
                                                   remains=False):
            self.calls.append('_process_quotas')
            result = {}
            for k, v in resources.items():
                reserved = 0
                if k == 'instances':
                    in_use = 1
                elif k == 'cores':
                    in_use = 5
                    reserved = 10
                else:
                    in_use = 0
                result[k] = {'limit': v.default,
                             'in_use': in_use, 'reserved': reserved}
            return result

        def fake_qgabpau(context, project_id, user_id):
            self.calls.append('quota_get_all_by_project_and_user')
            return {'instances': 2, 'cores': -1}

        self.stub_out('nova.db.quota_get_all_by_project',
                       fake_quota_get_all_by_project)
        self.stub_out('nova.quota.DbQuotaDriver.get_project_quotas',
                       fake_get_project_quotas)
        self.stub_out('nova.quota.DbQuotaDriver._process_quotas',
                       fake_process_quotas_in_get_user_quotas)
        self.stub_out('nova.db.quota_get_all_by_project_and_user',
                       fake_qgabpau)

    def test_get_settable_quotas_with_user(self):
        self._stub_get_settable_quotas()
        result = self.driver.get_settable_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', user_id='test_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'get_project_quotas',
                'quota_get_all_by_project_and_user',
                '_process_quotas',
                ])
        self.assertEqual(result, {
                'instances': {
                    'minimum': 1,
                    'maximum': 7,
                    },
                'cores': {
                    'minimum': 15,
                    'maximum': -1,
                    },
                'ram': {
                    'minimum': 0,
                    'maximum': 50 * 1024,
                    },
                'floating_ips': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'fixed_ips': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'metadata_items': {
                    'minimum': 0,
                    'maximum': 128,
                    },
                'injected_files': {
                    'minimum': 0,
                    'maximum': 5,
                    },
                'injected_file_content_bytes': {
                    'minimum': 0,
                    'maximum': 10 * 1024,
                    },
                'injected_file_path_bytes': {
                    'minimum': 0,
                    'maximum': 255,
                    },
                'security_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'security_group_rules': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'key_pairs': {
                    'minimum': 0,
                    'maximum': 100,
                    },
                'server_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'server_group_members': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                })

    def test_get_settable_quotas_without_user(self):
        self._stub_get_settable_quotas()
        result = self.driver.get_settable_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'get_project_quotas',
                ])
        self.assertEqual(result, {
                'instances': {
                    'minimum': 5,
                    'maximum': -1,
                    },
                'cores': {
                    'minimum': 5,
                    'maximum': -1,
                    },
                'ram': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'floating_ips': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'fixed_ips': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'metadata_items': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'injected_files': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'injected_file_content_bytes': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'injected_file_path_bytes': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'security_groups': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'security_group_rules': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'key_pairs': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'server_groups': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                'server_group_members': {
                    'minimum': 0,
                    'maximum': -1,
                    },
                })

    def test_get_settable_quotas_by_user_with_unlimited_value(self):
        self._stub_get_settable_quotas()
        result = self.driver.get_settable_quotas(
            FakeContext('test_project', 'test_class'),
            quota.QUOTAS._resources, 'test_project', user_id='test_user')

        self.assertEqual(self.calls, [
                'quota_get_all_by_project',
                'get_project_quotas',
                'quota_get_all_by_project_and_user',
                '_process_quotas',
                ])
        self.assertEqual(result, {
                'instances': {
                    'minimum': 1,
                    'maximum': 7,
                    },
                'cores': {
                    'minimum': 15,
                    'maximum': -1,
                    },
                'ram': {
                    'minimum': 0,
                    'maximum': 50 * 1024,
                    },
                'floating_ips': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'fixed_ips': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'metadata_items': {
                    'minimum': 0,
                    'maximum': 128,
                    },
                'injected_files': {
                    'minimum': 0,
                    'maximum': 5,
                    },
                'injected_file_content_bytes': {
                    'minimum': 0,
                    'maximum': 10 * 1024,
                    },
                'injected_file_path_bytes': {
                    'minimum': 0,
                    'maximum': 255,
                    },
                'security_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'security_group_rules': {
                    'minimum': 0,
                    'maximum': 20,
                    },
                'key_pairs': {
                    'minimum': 0,
                    'maximum': 100,
                    },
                'server_groups': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                'server_group_members': {
                    'minimum': 0,
                    'maximum': 10,
                    },
                })

    def _stub_get_project_quotas(self):
        def fake_get_project_quotas(dbdrv, context, resources, project_id,
                                    quota_class=None, defaults=True,
                                    usages=True, remains=False,
                                    project_quotas=None):
            self.calls.append('get_project_quotas')
            return {k: dict(limit=v.default) for k, v in resources.items()}

        self.stub_out('nova.quota.DbQuotaDriver.get_project_quotas',
                       fake_get_project_quotas)

    def test_get_quotas_has_sync_unknown(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.QuotaResourceUnknown,
                          self.driver._get_quotas,
                          None, quota.QUOTAS._resources,
                          ['unknown'], True)
        self.assertEqual(self.calls, [])

    def test_get_quotas_no_sync_unknown(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.QuotaResourceUnknown,
                          self.driver._get_quotas,
                          None, quota.QUOTAS._resources,
                          ['unknown'], False)
        self.assertEqual(self.calls, [])

    def test_get_quotas_has_sync_no_sync_resource(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.QuotaResourceUnknown,
                          self.driver._get_quotas,
                          None, quota.QUOTAS._resources,
                          ['metadata_items'], True)
        self.assertEqual(self.calls, [])

    def test_get_quotas_no_sync_has_sync_resource(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.QuotaResourceUnknown,
                          self.driver._get_quotas,
                          None, quota.QUOTAS._resources,
                          ['instances'], False)
        self.assertEqual(self.calls, [])

    def test_get_quotas_has_sync(self):
        self._stub_get_project_quotas()
        result = self.driver._get_quotas(FakeContext('test_project',
                                                     'test_class'),
                                         quota.QUOTAS._resources,
                                         ['instances', 'cores', 'ram',
                                          'floating_ips'],
                                         True,
                                         project_id='test_project')

        self.assertEqual(self.calls, ['get_project_quotas'])
        self.assertEqual(result, dict(
                instances=10,
                cores=20,
                ram=50 * 1024,
                floating_ips=10,
                ))

    def test_get_quotas_no_sync(self):
        self._stub_get_project_quotas()
        result = self.driver._get_quotas(FakeContext('test_project',
                                                     'test_class'),
                                         quota.QUOTAS._resources,
                                         ['metadata_items', 'injected_files',
                                          'injected_file_content_bytes',
                                          'injected_file_path_bytes',
                                          'security_group_rules',
                                          'server_group_members',
                                          'server_groups', 'security_groups'],
                                          False,
                                         project_id='test_project')

        self.assertEqual(self.calls, ['get_project_quotas'])
        self.assertEqual(result, dict(
                metadata_items=128,
                injected_files=5,
                injected_file_content_bytes=10 * 1024,
                injected_file_path_bytes=255,
                security_group_rules=20,
                server_group_members=10,
                server_groups=10,
                security_groups=10,
                ))

    def test_limit_check_under(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.InvalidQuotaValue,
                          self.driver.limit_check,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(metadata_items=-1))

    def test_limit_check_over(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(metadata_items=129))

    def test_limit_check_project_overs(self):
        self._stub_get_project_quotas()
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(injected_file_content_bytes=10241,
                               injected_file_path_bytes=256))

    def test_limit_check_unlimited(self):
        self.flags(metadata_items=-1, group='quota')
        self._stub_get_project_quotas()
        self.driver.limit_check(FakeContext('test_project', 'test_class'),
                                quota.QUOTAS._resources,
                                dict(metadata_items=32767))

    def test_limit_check(self):
        self._stub_get_project_quotas()
        self.driver.limit_check(FakeContext('test_project', 'test_class'),
                                quota.QUOTAS._resources,
                                dict(metadata_items=128))

    def test_limit_check_project_and_user_no_values(self):
        self.assertRaises(exception.Invalid,
                          self.driver.limit_check_project_and_user,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources)

    def test_limit_check_project_and_user_under(self):
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': -1}},
                  {'user_values': {'key_pairs': -1}},
                  {'project_values': {'instances': -1},
                   'user_values': {'instances': -1}}]
        for kwarg in kwargs:
            self.assertRaises(exception.InvalidQuotaValue,
                              self.driver.limit_check_project_and_user,
                              ctxt, resources, **kwarg)

    def test_limit_check_project_and_user_over_project(self):
        # Check the case where user_values pass user quota but project_values
        # exceed project quota.
        self.flags(instances=5, group='quota')
        self._stub_get_project_quotas()
        resources = self._get_fake_countable_resources()
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check_project_and_user,
                          FakeContext('test_project', 'test_class'),
                          resources,
                          project_values=dict(instances=6),
                          user_values=dict(instances=5))

    def test_limit_check_project_and_user_over_user(self):
        self.flags(instances=5, group='quota')
        self._stub_get_project_quotas()
        resources = self._get_fake_countable_resources()
        # It's not realistic for user_values to be higher than project_values,
        # but this is just for testing the fictional case where project_values
        # pass project quota but user_values exceed user quota.
        self.assertRaises(exception.OverQuota,
                          self.driver.limit_check_project_and_user,
                          FakeContext('test_project', 'test_class'),
                          resources,
                          project_values=dict(instances=5),
                          user_values=dict(instances=6))

    def test_limit_check_project_and_user_overs(self):
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 10241}},
                  {'user_values': {'key_pairs': 256}},
                  {'project_values': {'instances': 512},
                   'user_values': {'instances': 256}}]
        for kwarg in kwargs:
            self.assertRaises(exception.OverQuota,
                              self.driver.limit_check_project_and_user,
                              ctxt, resources, **kwarg)

    def test_limit_check_project_and_user_unlimited(self):
        self.flags(fixed_ips=-1, group='quota')
        self.flags(key_pairs=-1, group='quota')
        self.flags(instances=-1, group='quota')
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 32767}},
                  {'user_values': {'key_pairs': 32767}},
                  {'project_values': {'instances': 32767},
                   'user_values': {'instances': 32767}}]
        for kwarg in kwargs:
            self.driver.limit_check_project_and_user(ctxt, resources, **kwarg)

    def test_limit_check_project_and_user(self):
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 5}},
                  {'user_values': {'key_pairs': 5}},
                  {'project_values': {'instances': 5},
                   'user_values': {'instances': 5}}]
        for kwarg in kwargs:
            self.driver.limit_check_project_and_user(ctxt, resources, **kwarg)

    def test_limit_check_project_and_user_zero_values(self):
        """Tests to make sure that we don't compare 0 to None and fail with
        a TypeError in python 3 when calculating merged_values between
        project_values and user_values.
        """
        self._stub_get_project_quotas()
        ctxt = FakeContext('test_project', 'test_class')
        resources = self._get_fake_countable_resources()
        # Check: only project_values, only user_values, and then both.
        kwargs = [{'project_values': {'fixed_ips': 0}},
                  {'user_values': {'key_pairs': 0}},
                  {'project_values': {'instances': 0},
                   'user_values': {'instances': 0}}]
        for kwarg in kwargs:
            self.driver.limit_check_project_and_user(ctxt, resources, **kwarg)

    def _stub_quota_reserve(self):
        def fake_quota_reserve(context, resources, quotas, user_quotas, deltas,
                               expire, until_refresh, max_age, project_id=None,
                               user_id=None):
            self.calls.append(('quota_reserve', expire, until_refresh,
                               max_age))
            return ['resv-1', 'resv-2', 'resv-3']
        self.stub_out('nova.db.quota_reserve', fake_quota_reserve)

    def test_reserve_bad_expire(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        self.assertRaises(exception.InvalidReservationExpiration,
                          self.driver.reserve,
                          FakeContext('test_project', 'test_class'),
                          quota.QUOTAS._resources,
                          dict(instances=2), expire='invalid')
        self.assertEqual(self.calls, [])

    def test_reserve_default_expire(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        result = self.driver.reserve(FakeContext('test_project', 'test_class'),
                                     quota.QUOTAS._resources,
                                     dict(instances=2))

        expire = timeutils.utcnow() + datetime.timedelta(seconds=86400)
        self.assertEqual(self.calls, [
                'get_project_quotas',
                ('quota_reserve', expire, 0, 0),
                ])
        self.assertEqual(result, ['resv-1', 'resv-2', 'resv-3'])

    def test_reserve_int_expire(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        result = self.driver.reserve(FakeContext('test_project', 'test_class'),
                                     quota.QUOTAS._resources,
                                     dict(instances=2), expire=3600)

        expire = timeutils.utcnow() + datetime.timedelta(seconds=3600)
        self.assertEqual(self.calls, [
                'get_project_quotas',
                ('quota_reserve', expire, 0, 0),
                ])
        self.assertEqual(result, ['resv-1', 'resv-2', 'resv-3'])

    def test_reserve_timedelta_expire(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        expire_delta = datetime.timedelta(seconds=60)
        result = self.driver.reserve(FakeContext('test_project', 'test_class'),
                                     quota.QUOTAS._resources,
                                     dict(instances=2), expire=expire_delta)

        expire = timeutils.utcnow() + expire_delta
        self.assertEqual(self.calls, [
                'get_project_quotas',
                ('quota_reserve', expire, 0, 0),
                ])
        self.assertEqual(result, ['resv-1', 'resv-2', 'resv-3'])

    def test_reserve_datetime_expire(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        expire = timeutils.utcnow() + datetime.timedelta(seconds=120)
        result = self.driver.reserve(FakeContext('test_project', 'test_class'),
                                     quota.QUOTAS._resources,
                                     dict(instances=2), expire=expire)

        self.assertEqual(self.calls, [
                'get_project_quotas',
                ('quota_reserve', expire, 0, 0),
                ])
        self.assertEqual(result, ['resv-1', 'resv-2', 'resv-3'])

    def test_reserve_until_refresh(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        self.flags(until_refresh=500, group='quota')
        expire = timeutils.utcnow() + datetime.timedelta(seconds=120)
        result = self.driver.reserve(FakeContext('test_project', 'test_class'),
                                     quota.QUOTAS._resources,
                                     dict(instances=2), expire=expire)

        self.assertEqual(self.calls, [
                'get_project_quotas',
                ('quota_reserve', expire, 500, 0),
                ])
        self.assertEqual(result, ['resv-1', 'resv-2', 'resv-3'])

    def test_reserve_max_age(self):
        self._stub_get_project_quotas()
        self._stub_quota_reserve()
        self.flags(max_age=86400, group='quota')
        expire = timeutils.utcnow() + datetime.timedelta(seconds=120)
        result = self.driver.reserve(FakeContext('test_project', 'test_class'),
                                     quota.QUOTAS._resources,
                                     dict(instances=2), expire=expire)

        self.assertEqual(self.calls, [
                'get_project_quotas',
                ('quota_reserve', expire, 0, 86400),
                ])
        self.assertEqual(result, ['resv-1', 'resv-2', 'resv-3'])

    def test_usage_reset(self):
        calls = []

        def fake_quota_usage_update(context, project_id, user_id, resource,
                                    **kwargs):
            calls.append(('quota_usage_update', context, project_id, user_id,
                          resource, kwargs))
            if resource == 'nonexist':
                raise exception.QuotaUsageNotFound(project_id=project_id)
        self.stub_out('nova.db.quota_usage_update', fake_quota_usage_update)

        ctx = FakeContext('test_project', 'test_class')
        resources = ['res1', 'res2', 'nonexist', 'res4']
        self.driver.usage_reset(ctx, resources)

        # Make sure we had some calls
        self.assertEqual(len(calls), len(resources))

        # Extract the elevated context that was used and do some
        # sanity checks
        elevated = calls[0][1]
        self.assertEqual(elevated.project_id, ctx.project_id)
        self.assertEqual(elevated.quota_class, ctx.quota_class)
        self.assertTrue(elevated.is_admin)

        # Now check that all the expected calls were made
        exemplar = [('quota_usage_update', elevated, 'test_project',
                     'fake_user', res, dict(in_use=-1)) for res in resources]
        self.assertEqual(calls, exemplar)


class FakeSession(object):
    def begin(self):
        return self

    def add(self, instance):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        return False


class FakeUsage(sqa_models.QuotaUsage):
    def save(self, *args, **kwargs):
        pass


class QuotaSqlAlchemyBase(test.TestCase):
    def setUp(self):
        super(QuotaSqlAlchemyBase, self).setUp()
        self.sync_called = set()
        self.quotas = dict(
            instances=5,
            cores=10,
            ram=10 * 1024,
            )
        self.deltas = dict(
            instances=2,
            cores=4,
            ram=2 * 1024,
            )

        def make_sync(res_name):
            def sync(context, project_id, user_id):
                self.sync_called.add(res_name)
                if res_name in self.usages:
                    if self.usages[res_name].in_use < 0:
                        return {res_name: 2}
                    else:
                        return {res_name: self.usages[res_name].in_use - 1}
                return {res_name: 0}
            return sync
        self.resources = {}

        _existing_quota_sync_func_dict = dict(sqa_api.QUOTA_SYNC_FUNCTIONS)

        def restore_sync_functions():
            sqa_api.QUOTA_SYNC_FUNCTIONS.clear()
            sqa_api.QUOTA_SYNC_FUNCTIONS.update(_existing_quota_sync_func_dict)

        self.addCleanup(restore_sync_functions)

        for res_name in ('instances', 'cores', 'ram', 'floating_ips'):
            method_name = '_sync_%s' % res_name
            sqa_api.QUOTA_SYNC_FUNCTIONS[method_name] = make_sync(res_name)
            res = quota.ReservableResource(res_name, '_sync_%s' % res_name)
            self.resources[res_name] = res

        self.expire = timeutils.utcnow() + datetime.timedelta(seconds=3600)
        self.usages = {}
        self.usages_created = {}
        self.reservations_created = {}
        self.usages_list = [
                dict(resource='instances',
                     project_id='test_project',
                     user_id='fake_user',
                     in_use=2,
                     reserved=2,
                     until_refresh=None),
                dict(resource='cores',
                     project_id='test_project',
                     user_id='fake_user',
                     in_use=2,
                     reserved=4,
                     until_refresh=None),
                dict(resource='ram',
                     project_id='test_project',
                     user_id='fake_user',
                     in_use=2,
                     reserved=2 * 1024,
                     until_refresh=None),
                ]

        def fake_get_project_user_quota_usages(context, project_id, user_id):
            return self.usages.copy(), self.usages.copy()

        def fake_quota_usage_create(project_id, user_id, resource,
                                    in_use, reserved, until_refresh,
                                    session):
            quota_usage_ref = self._make_quota_usage(
                project_id, user_id, resource, in_use, reserved, until_refresh,
                timeutils.utcnow(), timeutils.utcnow())

            self.usages_created[resource] = quota_usage_ref

            return quota_usage_ref

        def fake_reservation_create(uuid, usage_id, project_id,
                                    user_id, resource, delta, expire,
                                    session):
            reservation_ref = self._make_reservation(
                uuid, usage_id, project_id, user_id, resource, delta, expire,
                timeutils.utcnow(), timeutils.utcnow())

            self.reservations_created[resource] = reservation_ref

            return reservation_ref

        self.stub_out('nova.db.sqlalchemy.api._get_project_user_quota_usages',
                       fake_get_project_user_quota_usages)
        self.stub_out('nova.db.sqlalchemy.api._quota_usage_create',
                       fake_quota_usage_create)
        self.stub_out('nova.db.sqlalchemy.api._reservation_create',
                       fake_reservation_create)

        self.useFixture(test.TimeOverride())

    def _make_quota_usage(self, project_id, user_id, resource, in_use,
                          reserved, until_refresh, created_at, updated_at):
        quota_usage_ref = FakeUsage()
        quota_usage_ref.id = len(self.usages) + len(self.usages_created)
        quota_usage_ref.project_id = project_id
        quota_usage_ref.user_id = user_id
        quota_usage_ref.resource = resource
        quota_usage_ref.in_use = in_use
        quota_usage_ref.reserved = reserved
        quota_usage_ref.until_refresh = until_refresh
        quota_usage_ref.created_at = created_at
        quota_usage_ref.updated_at = updated_at
        quota_usage_ref.deleted_at = None
        quota_usage_ref.deleted = False

        return quota_usage_ref

    def init_usage(self, project_id, user_id, resource, in_use, reserved=0,
                   until_refresh=None, created_at=None, updated_at=None):
        if created_at is None:
            created_at = timeutils.utcnow()
        if updated_at is None:
            updated_at = timeutils.utcnow()
        if resource == 'fixed_ips' or resource == 'floating_ips':
            user_id = None

        quota_usage_ref = self._make_quota_usage(project_id, user_id, resource,
                                                 in_use, reserved,
                                                 until_refresh,
                                                 created_at, updated_at)

        self.usages[resource] = quota_usage_ref

    def compare_usage(self, usage_dict, expected):
        for usage in expected:
            resource = usage['resource']
            for key, value in usage.items():
                actual = getattr(usage_dict[resource], key)
                self.assertEqual(actual, value,
                                 "%s != %s on usage for resource %s, key %s" %
                                 (actual, value, resource, key))

    def _make_reservation(self, uuid, usage_id, project_id, user_id, resource,
                          delta, expire, created_at, updated_at):
        reservation_ref = sqa_models.Reservation()
        reservation_ref.id = len(self.reservations_created)
        reservation_ref.uuid = uuid
        reservation_ref.usage_id = usage_id
        reservation_ref.project_id = project_id
        reservation_ref.user_id = user_id
        reservation_ref.resource = resource
        reservation_ref.delta = delta
        reservation_ref.expire = expire
        reservation_ref.created_at = created_at
        reservation_ref.updated_at = updated_at
        reservation_ref.deleted_at = None
        reservation_ref.deleted = False

        return reservation_ref

    def compare_reservation(self, reservations, expected):
        reservations = set(reservations)
        for resv in expected:
            resource = resv['resource']
            resv_obj = self.reservations_created[resource]

            self.assertIn(resv_obj.uuid, reservations)
            reservations.discard(resv_obj.uuid)

            for key, value in resv.items():
                actual = getattr(resv_obj, key)
                self.assertEqual(actual, value,
                                 "%s != %s on reservation for resource %s" %
                                 (actual, value, resource))

        self.assertEqual(len(reservations), 0)

    def _update_reservations_list(self, usage_id_change=False,
                                  delta_change=False):
        reservations_list = [
            dict(resource='instances',
                project_id='test_project',
                delta=2),
            dict(resource='cores',
                project_id='test_project',
                delta=4),
            dict(resource='ram',
                delta=2 * 1024),
            ]
        if usage_id_change:
            reservations_list[0]["usage_id"] = self.usages_created['instances']
            reservations_list[1]["usage_id"] = self.usages_created['cores']
            reservations_list[2]["usage_id"] = self.usages_created['ram']
        else:
            reservations_list[0]["usage_id"] = self.usages['instances']
            reservations_list[1]["usage_id"] = self.usages['cores']
            reservations_list[2]["usage_id"] = self.usages['ram']
        if delta_change:
            reservations_list[0]["delta"] = -2
            reservations_list[1]["delta"] = -4
            reservations_list[2]["delta"] = -2 * 1024
        return reservations_list

    def _init_usages(self, *in_use, **kwargs):
        for i, option in enumerate(('instances', 'cores', 'ram')):
            self.init_usage('test_project', 'fake_user',
                            option, in_use[i], **kwargs)
        return FakeContext('test_project', 'test_class')


class QuotaReserveSqlAlchemyTestCase(QuotaSqlAlchemyBase):
    # nova.db.sqlalchemy.api.quota_reserve is so complex it needs its
    # own test case, and since it's a quota manipulator, this is the
    # best place to put it...

    def test_quota_reserve_create_usages(self):
        context = FakeContext('test_project', 'test_class')
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       0, 0)

        self.assertEqual(self.sync_called, set(['instances', 'cores',
                                                'ram']))
        self.usages_list[0]["in_use"] = 0
        self.usages_list[1]["in_use"] = 0
        self.usages_list[2]["in_use"] = 0
        self.compare_usage(self.usages_created, self.usages_list)
        reservations_list = self._update_reservations_list(True)
        self.compare_reservation(result, reservations_list)

    def test_quota_reserve_negative_in_use(self):
        context = self._init_usages(-1, -1, -1, -1, until_refresh=1)
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       5, 0)

        self.assertEqual(self.sync_called, set(['instances', 'cores',
                                                'ram']))
        self.usages_list[0]["until_refresh"] = 5
        self.usages_list[1]["until_refresh"] = 5
        self.usages_list[2]["until_refresh"] = 5
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.compare_reservation(result, self._update_reservations_list())

    def test_quota_reserve_until_refresh(self):
        context = self._init_usages(3, 3, 3, 3, until_refresh=1)
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       5, 0)

        self.assertEqual(self.sync_called, set(['instances', 'cores',
                                                'ram']))
        self.usages_list[0]["until_refresh"] = 5
        self.usages_list[1]["until_refresh"] = 5
        self.usages_list[2]["until_refresh"] = 5
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.compare_reservation(result, self._update_reservations_list())

    def test_quota_reserve_max_age(self):
        max_age = 3600
        record_created = (timeutils.utcnow() -
                          datetime.timedelta(seconds=max_age))
        context = self._init_usages(3, 3, 3, 3, created_at=record_created,
                                    updated_at=record_created)
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       0, max_age)

        self.assertEqual(self.sync_called, set(['instances', 'cores',
                                                'ram']))
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.compare_reservation(result, self._update_reservations_list())

    def test_quota_reserve_no_refresh(self):
        context = self._init_usages(3, 3, 3, 3)
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       0, 0)

        self.assertEqual(self.sync_called, set([]))
        self.usages_list[0]["in_use"] = 3
        self.usages_list[1]["in_use"] = 3
        self.usages_list[2]["in_use"] = 3
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.compare_reservation(result, self._update_reservations_list())

    def test_quota_reserve_unders(self):
        context = self._init_usages(1, 3, 1 * 1024, 1)
        self.deltas["instances"] = -2
        self.deltas["cores"] = -4
        self.deltas["ram"] = -2 * 1024
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       0, 0)

        self.assertEqual(self.sync_called, set([]))
        self.usages_list[0]["in_use"] = 1
        self.usages_list[0]["reserved"] = 0
        self.usages_list[1]["in_use"] = 3
        self.usages_list[1]["reserved"] = 0
        self.usages_list[2]["in_use"] = 1 * 1024
        self.usages_list[2]["reserved"] = 0
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        reservations_list = self._update_reservations_list(False, True)
        self.compare_reservation(result, reservations_list)

    def test_quota_reserve_overs(self):
        context = self._init_usages(4, 8, 10 * 1024, 4)
        try:
            sqa_api.quota_reserve(context, self.resources, self.quotas,
                          self.quotas, self.deltas, self.expire, 0, 0)
        except exception.OverQuota as e:
            expected_kwargs = {'code': 500,
                'usages': {'instances': {'reserved': 0, 'in_use': 4},
                'ram': {'reserved': 0, 'in_use': 10240},
                'cores': {'reserved': 0, 'in_use': 8}},
                'overs': ['cores', 'instances', 'ram'],
                'quotas': {'cores': 10, 'ram': 10240,
                           'instances': 5}}
            self.assertEqual(e.kwargs, expected_kwargs)
        else:
            self.fail('Expected OverQuota failure')
        self.assertEqual(self.sync_called, set([]))
        self.usages_list[0]["in_use"] = 4
        self.usages_list[0]["reserved"] = 0
        self.usages_list[1]["in_use"] = 8
        self.usages_list[1]["reserved"] = 0
        self.usages_list[2]["in_use"] = 10 * 1024
        self.usages_list[2]["reserved"] = 0
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.assertEqual(self.reservations_created, {})

    def test_quota_reserve_cores_unlimited(self):
        # Requesting 8 cores, quota_cores set to unlimited:
        self.flags(cores=-1, group='quota')
        self._init_usages(1, 8, 1 * 1024, 1)
        self.assertEqual(self.sync_called, set([]))
        self.usages_list[0]["in_use"] = 1
        self.usages_list[0]["reserved"] = 0
        self.usages_list[1]["in_use"] = 8
        self.usages_list[1]["reserved"] = 0
        self.usages_list[2]["in_use"] = 1 * 1024
        self.usages_list[2]["reserved"] = 0
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.assertEqual(self.reservations_created, {})

    def test_quota_reserve_ram_unlimited(self):
        # Requesting 10*1024 ram, quota_ram set to unlimited:
        self.flags(ram=-1, group='quota')
        self._init_usages(1, 1, 10 * 1024, 1)
        self.assertEqual(self.sync_called, set([]))
        self.usages_list[0]["in_use"] = 1
        self.usages_list[0]["reserved"] = 0
        self.usages_list[1]["in_use"] = 1
        self.usages_list[1]["reserved"] = 0
        self.usages_list[2]["in_use"] = 10 * 1024
        self.usages_list[2]["reserved"] = 0
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        self.assertEqual(self.reservations_created, {})

    def test_quota_reserve_reduction(self):
        context = self._init_usages(10, 20, 20 * 1024, 10)
        self.deltas["instances"] = -2
        self.deltas["cores"] = -4
        self.deltas["ram"] = -2 * 1024
        result = sqa_api.quota_reserve(context, self.resources, self.quotas,
                                       self.quotas, self.deltas, self.expire,
                                       0, 0)

        self.assertEqual(self.sync_called, set([]))
        self.usages_list[0]["in_use"] = 10
        self.usages_list[0]["reserved"] = 0
        self.usages_list[1]["in_use"] = 20
        self.usages_list[1]["reserved"] = 0
        self.usages_list[2]["in_use"] = 20 * 1024
        self.usages_list[2]["reserved"] = 0
        self.compare_usage(self.usages, self.usages_list)
        self.assertEqual(self.usages_created, {})
        reservations_list = self._update_reservations_list(False, True)
        self.compare_reservation(result, reservations_list)


class QuotaEngineUsageRefreshTestCase(QuotaSqlAlchemyBase):
    def _init_usages(self, *in_use, **kwargs):
        for i, option in enumerate(('instances', 'cores', 'ram',
                                    'floating_ips')):
            self.init_usage('test_project', 'fake_user',
                            option, in_use[i], **kwargs)
        return FakeContext('test_project', 'test_class')

    def setUp(self):
        super(QuotaEngineUsageRefreshTestCase, self).setUp()

        # The usages_list are the expected usages (in_use) values after
        # the test has run.
        # The pattern is that the test will initialize the actual in_use
        # to 3 for all the resources, then the refresh will sync
        # the actual in_use to 2 for the resources whose names are in the keys
        # list and are scoped to project or user.

        # The usages are indexed as follows:
        # Index Resource name    Scope
        # 0     instances        user
        # 1     cores            user
        # 2     ram              user
        # 3     floating_ips     project
        self.usages_list.append(dict(resource='floating_ips',
                     project_id='test_project',
                     user_id=None,
                     in_use=2,
                     reserved=2,
                     until_refresh=None))

        # None of the usage refresh tests should add a reservation.
        self.usages_list[0]['reserved'] = 0
        self.usages_list[1]['reserved'] = 0
        self.usages_list[2]['reserved'] = 0
        self.usages_list[3]['reserved'] = 0

        def fake_quota_get_all_by_project_and_user(context, project_id,
                                                   user_id):
            return self.quotas

        def fake_quota_get_all_by_project(context, project_id):
            return self.quotas

        self.stub_out('nova.db.sqlalchemy.api.quota_get_all_by_project',
                       fake_quota_get_all_by_project)
        self.stub_out(
            'nova.db.sqlalchemy.api.quota_get_all_by_project_and_user',
            fake_quota_get_all_by_project_and_user)

        # The actual sync function for instances, ram, and cores, is
        # _sync_instances, so override the function here.
        def make_instances_sync():
            def sync(context, project_id, user_id):
                updates = {}
                self.sync_called.add('instances')

                for res_name in ('instances', 'cores', 'ram'):
                    if res_name not in self.usages:
                        # Usage doesn't exist yet, initialize
                        # the in_use to 0.
                        updates[res_name] = 0
                    elif self.usages[res_name].in_use < 0:
                        updates[res_name] = 2
                    else:
                        # Simulate as if the actual usage
                        # is one less than the recorded usage.
                        updates[res_name] = \
                            self.usages[res_name].in_use - 1
                return updates
            return sync

        sqa_api.QUOTA_SYNC_FUNCTIONS['_sync_instances'] = make_instances_sync()

    def test_usage_refresh_user_all_keys(self):
        self._init_usages(3, 3, 3, 3, 3, 3, 3, until_refresh = 5)
        # Let the parameters determine the project_id and user_id,
        # not the context.
        ctxt = context.get_admin_context()
        quota.QUOTAS.usage_refresh(ctxt, 'test_project', 'fake_user')

        self.assertEqual(self.sync_called, set(['instances']))

        # Compare the expected usages with the actual usages.
        # Expect floating_ips not to change since it is project scoped.
        self.usages_list[3]['in_use'] = 3
        self.usages_list[3]['until_refresh'] = 5
        self.compare_usage(self.usages, self.usages_list)

        # No usages were created.
        self.assertEqual(self.usages_created, {})

    def test_usage_refresh_user_one_key(self):
        context = self._init_usages(3, 3, 3, 3, 3, 3, 3,
                                    until_refresh = 5)
        keys = ['ram']
        # Let the context determine the project_id and user_id
        quota.QUOTAS.usage_refresh(context, None, None, keys)

        self.assertEqual(self.sync_called, set(['instances']))

        # Compare the expected usages with the actual usages.
        # Expect floating_ips not to change since it is project scoped.
        self.usages_list[3]['in_use'] = 3
        self.usages_list[3]['until_refresh'] = 5
        self.compare_usage(self.usages, self.usages_list)

        # No usages were created.
        self.assertEqual(self.usages_created, {})

    def test_usage_refresh_create_user_usage(self):
        context = FakeContext('test_project', 'test_class')

        # Create per-user ram usage
        keys = ['ram']
        quota.QUOTAS.usage_refresh(context, 'test_project', 'fake_user', keys)

        self.assertEqual(self.sync_called, set(['instances']))

        # Compare the expected usages with the created usages.
        # Expect instances to be created and initialized to 0
        self.usages_list[0]['in_use'] = 0
        # Expect cores to be created and initialized to 0
        self.usages_list[1]['in_use'] = 0
        # Expect ram to be created and initialized to 0
        self.usages_list[2]['in_use'] = 0
        self.compare_usage(self.usages_created, self.usages_list[0:3])

        self.assertEqual(len(self.usages_created), 3)

    def test_usage_refresh_project_all_keys(self):
        self._init_usages(3, 3, 3, 3, 3, 3, 3, until_refresh = 5)
        # Let the parameter determine the project_id, not the context.
        ctxt = context.get_admin_context()
        quota.QUOTAS.usage_refresh(ctxt, 'test_project')

        self.assertEqual(self.sync_called, set(['floating_ips']))

        # Compare the expected usages with the actual usages.
        # Expect instances not to change since it is user scoped.
        self.usages_list[0]['in_use'] = 3
        self.usages_list[0]['until_refresh'] = 5
        # Expect cores not to change since it is user scoped.
        self.usages_list[1]['in_use'] = 3
        self.usages_list[1]['until_refresh'] = 5
        # Expect ram not to change since it is user scoped.
        self.usages_list[2]['in_use'] = 3
        self.usages_list[2]['until_refresh'] = 5
        self.compare_usage(self.usages, self.usages_list)

        self.assertEqual(self.usages_created, {})

    def test_usage_refresh_project_one_key(self):
        self._init_usages(3, 3, 3, 3, 3, 3, 3, until_refresh = 5)
        # Let the parameter determine the project_id, not the context.
        ctxt = context.get_admin_context()
        keys = ['floating_ips']
        quota.QUOTAS.usage_refresh(ctxt, 'test_project', resource_names=keys)

        self.assertEqual(self.sync_called, set(['floating_ips']))

        # Compare the expected usages with the actual usages.
        # Expect instances not to change since it is user scoped.
        self.usages_list[0]['in_use'] = 3
        self.usages_list[0]['until_refresh'] = 5
        # Expect cores not to change since it is user scoped.
        self.usages_list[1]['in_use'] = 3
        self.usages_list[1]['until_refresh'] = 5
        # Expect ram not to change since it is user scoped.
        self.usages_list[2]['in_use'] = 3
        self.usages_list[2]['until_refresh'] = 5
        self.compare_usage(self.usages, self.usages_list)

        self.assertEqual(self.usages_created, {})

    def test_usage_refresh_create_project_usage(self):
        ctxt = context.get_admin_context()

        # Create per-project floating_ips usage
        keys = ['floating_ips']
        quota.QUOTAS.usage_refresh(ctxt, 'test_project', resource_names=keys)

        self.assertEqual(self.sync_called, set(['floating_ips']))

        # Compare the expected usages with the created usages.
        # Expect floating_ips to be created and initialized to 0
        self.usages_list[3]['in_use'] = 0
        self.compare_usage(self.usages_created, self.usages_list[3:])

        self.assertEqual(len(self.usages_created), 1)

    def _test_exception(self, context, project_id, user_id, keys):
        try:
            quota.QUOTAS.usage_refresh(context, project_id, user_id, keys)
        except exception.QuotaUsageRefreshNotAllowed as e:
            self.assertIn(keys[0], e.format_message())
        else:
            self.fail('Expected QuotaUsageRefreshNotAllowed failure')

    def test_usage_refresh_invalid_user_key(self):
        context = FakeContext('test_project', 'test_class')
        # floating_ips is a valid syncable project key,
        # but not a valid user key
        self._test_exception(context, 'test_project', 'fake_user',
                             ['floating_ips'])

    def test_usage_refresh_non_syncable_user_key(self):
        # security_group_rules is a valid user key, but not syncable
        context = FakeContext('test_project', 'test_class')
        self._test_exception(context, 'test_project', 'fake_user',
                             ['security_group_rules'])

    def test_usage_refresh_invalid_project_key(self):
        ctxt = context.get_admin_context()
        # ram is a valid syncable user key, but not a valid project key
        self._test_exception(ctxt, "test_project", None, ['ram'])

    def test_usage_refresh_non_syncable_project_key(self):
        # injected_files is a valid project key, but not syncable
        ctxt = context.get_admin_context()
        self._test_exception(ctxt, 'test_project', None, ['injected_files'])


class NoopQuotaDriverTestCase(test.TestCase):
    def setUp(self):
        super(NoopQuotaDriverTestCase, self).setUp()

        self.flags(instances=10,
                   cores=20,
                   ram=50 * 1024,
                   floating_ips=10,
                   metadata_items=128,
                   injected_files=5,
                   injected_file_content_bytes=10 * 1024,
                   injected_file_path_length=255,
                   security_groups=10,
                   security_group_rules=20,
                   reservation_expire=86400,
                   until_refresh=0,
                   max_age=0,
                   group='quota'
                   )

        self.expected_with_usages = {}
        self.expected_without_usages = {}
        self.expected_without_dict = {}
        self.expected_settable_quotas = {}
        for r in quota.QUOTAS._resources:
            self.expected_with_usages[r] = dict(limit=-1,
                                                in_use=-1,
                                                reserved=-1)
            self.expected_without_usages[r] = dict(limit=-1)
            self.expected_without_dict[r] = -1
            self.expected_settable_quotas[r] = dict(minimum=0, maximum=-1)

        self.driver = quota.NoopQuotaDriver()

    def test_get_defaults(self):
        # Use our pre-defined resources
        result = self.driver.get_defaults(None, quota.QUOTAS._resources)
        self.assertEqual(self.expected_without_dict, result)

    def test_get_class_quotas(self):
        result = self.driver.get_class_quotas(None,
                                              quota.QUOTAS._resources,
                                              'test_class')
        self.assertEqual(self.expected_without_dict, result)

    def test_get_class_quotas_no_defaults(self):
        result = self.driver.get_class_quotas(None,
                                              quota.QUOTAS._resources,
                                              'test_class',
                                              False)
        self.assertEqual(self.expected_without_dict, result)

    def test_get_project_quotas(self):
        result = self.driver.get_project_quotas(None,
                                                quota.QUOTAS._resources,
                                                'test_project')
        self.assertEqual(self.expected_with_usages, result)

    def test_get_user_quotas(self):
        result = self.driver.get_user_quotas(None,
                                             quota.QUOTAS._resources,
                                             'test_project',
                                             'fake_user')
        self.assertEqual(self.expected_with_usages, result)

    def test_get_project_quotas_no_defaults(self):
        result = self.driver.get_project_quotas(None,
                                                quota.QUOTAS._resources,
                                                'test_project',
                                                defaults=False)
        self.assertEqual(self.expected_with_usages, result)

    def test_get_user_quotas_no_defaults(self):
        result = self.driver.get_user_quotas(None,
                                             quota.QUOTAS._resources,
                                             'test_project',
                                             'fake_user',
                                             defaults=False)
        self.assertEqual(self.expected_with_usages, result)

    def test_get_project_quotas_no_usages(self):
        result = self.driver.get_project_quotas(None,
                                                quota.QUOTAS._resources,
                                                'test_project',
                                                usages=False)
        self.assertEqual(self.expected_without_usages, result)

    def test_get_user_quotas_no_usages(self):
        result = self.driver.get_user_quotas(None,
                                             quota.QUOTAS._resources,
                                             'test_project',
                                             'fake_user',
                                             usages=False)
        self.assertEqual(self.expected_without_usages, result)

    def test_get_settable_quotas_with_user(self):
        result = self.driver.get_settable_quotas(None,
                                                 quota.QUOTAS._resources,
                                                 'test_project',
                                                 'fake_user')
        self.assertEqual(self.expected_settable_quotas, result)

    def test_get_settable_quotas_without_user(self):
        result = self.driver.get_settable_quotas(None,
                                                 quota.QUOTAS._resources,
                                                 'test_project')
        self.assertEqual(self.expected_settable_quotas, result)
