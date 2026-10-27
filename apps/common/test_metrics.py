from django.test import Client
from redis.exceptions import RedisError

from apps.common import metrics


def test_the_metrics_endpoint_answers_inside_the_network(db):
    response = Client().get("/internal/metrics")
    assert response.status_code == 200
    body = response.content.decode()
    assert "railrush_scrape_ok" in body
    assert "# TYPE railrush_payment_events_total counter" in body


def test_a_request_that_came_through_nginx_is_refused(db):
    response = Client().get("/internal/metrics", HTTP_X_FORWARDED_FOR="203.0.113.9")
    assert response.status_code == 404


def test_a_failing_source_is_reported_not_raised(db, monkeypatch):
    def broken(self, runs):
        raise RedisError("redis is down")

    monkeypatch.setattr(metrics.StateCollector, "_queues", broken)
    response = Client().get("/internal/metrics")
    assert response.status_code == 200
    assert 'railrush_scrape_ok{source="redis"} 0.0' in response.content.decode()
