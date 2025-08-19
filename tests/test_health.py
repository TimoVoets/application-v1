import pytest
from httpx import AsyncClient

app_module = pytest.importorskip("app")
app = app_module.app


@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
