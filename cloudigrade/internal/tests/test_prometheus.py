"""Collection of tests for the internal.prometheus module."""
import itertools
from unittest.mock import patch

from django.test import TestCase


class InternalPrometheusTestCase(TestCase):
    """
    Test internal.prometheus.initialize_cached_metrics.

    This is a difficult and strange thing to test because normally a call to
    initialize_cached_metrics happens in InternalAppConfig.ready, but it appears to be
    impossible (within reason) to patch that behavior so that we override or mock it
    before Django tests actually run. Unfortunately, the AppConfig.ready functions are
    all called long before the test classes are loaded.

    So, we assume that InternalAppConfig.ready() may *already* have been called before
    these test functions even begin their setup.
    """

    def test_initialize_cached_metrics(self):
        """
        Test initialize_cached_metrics creates the expected Gauge objects.

        Since we assume that "ready" may already have been called, we don't actually
        need to call initialize_cached_metrics. We just assert the expected side-effects
        of calling initialize_cached_metrics.
        """
        from internal import prometheus
        from prometheus_client import registry

        self.assertEqual(
            len(prometheus.CACHED_GAUGE_METRICS_INFO),
            len(prometheus.CachedMetricsRegistry().get_registered_metrics_names()),
        )

        # Yes, _collector_to_names is pseudo-protected, but I couldn't find any other
        # supported mechanism to list all registered collectors.
        registered_names = itertools.chain(
            *list(registry.REGISTRY._collector_to_names.values())
        )
        for info in prometheus.CACHED_GAUGE_METRICS_INFO:
            self.assertIn(info.metric_name, registered_names)

    def test_initialize_cached_metrics_multiple_calls(self):
        """
        Test initialize_cached_metrics only creates the Gauge objects once.

        Since we assume that "ready" may already have been called, we patch two things:
        the _gauge_metrics dict that contains known created metrics and the
        imported Gauge class. The former requires patching because it was already
        populated with entries by the ready function, and the latter requires patching
        because instantiating a new Gauge instance affects a *global* registry within
        prometheus_client that we must not affect within the scope of a test.
        """
        patched_custom_gauge_metrics = {}
        with patch(
            "internal.prometheus.CachedMetricsRegistry._gauge_metrics",
            patched_custom_gauge_metrics,
        ), patch("internal.prometheus.Gauge") as mock_gauge:
            from internal import prometheus

            expected_final_count = len(prometheus.CACHED_GAUGE_METRICS_INFO)

            registry = prometheus.CachedMetricsRegistry()

            # Initially _gauge_metrics should be empty.
            self.assertEqual(0, len(registry.get_registered_metrics_names()))

            registry.initialize()
            metrics_after_first_call = registry.get_registered_metrics_names()
            # After one call, _gauge_metrics should be fully loaded.
            self.assertEqual(expected_final_count, len(metrics_after_first_call))
            self.assertEqual(expected_final_count, mock_gauge.call_count)

            mock_gauge.reset_mock()

            registry.initialize()
            # After the second call, _gauge_metrics should be unchanged,
            # and there should be no more Gauge calls.
            metrics_after_second_call = registry.get_registered_metrics_names()
            self.assertEqual(expected_final_count, len(metrics_after_second_call))
            self.assertEqual(metrics_after_first_call, metrics_after_second_call)
            self.assertEqual(0, mock_gauge.call_count)
