
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("INVOICE_NINJA_URL", "").rstrip("/")
API_TOKEN = os.getenv("INVOICE_NINJA_API_TOKEN", "")

HEADERS = {
    "X-API-TOKEN": API_TOKEN,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class InvoiceNinjaAPI:
    def __init__(self):
        if not BASE_URL or not API_TOKEN:
            raise ValueError("Invoice Ninja settings are missing in .env")

        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=HEADERS,
            timeout=30.0
        )

    async def get(self, endpoint, params=None):
        response = await self.client.get(
            endpoint,
            params=params
        )
        response.raise_for_status()
        return response.json()

    async def post(self, endpoint, data):
        response = await self.client.post(
            endpoint,
            json=data
        )
        response.raise_for_status()
        return response.json()

    async def get_clients(self, page=1):
        return await self.get(
            "/clients",
            params={"per_page": 100, "page": page}
        )

    async def get_products(self, page=1):
        return await self.get(
            "/products",
            params={"per_page": 100, "page": page}
        )

    async def get_client(self, client_id):
        return await self.get(f"/clients/{client_id}")

    async def create_client(self, payload):
        return await self.post("/clients", payload)

    async def get_invoice(self, invoice_id):
        return await self.get(f"/invoices/{invoice_id}")

    async def create_invoice(self, payload):
        return await self.post("/invoices", payload)

    async def search_invoices(self, query):
        result = await self.get(
            "/invoices",
            params={"filter": query, "per_page": 100}
        )
        return result.get("data", [])

    async def download_invoice_pdf(self, invoice_id):
        response = await self.client.get(
            f"/invoices/{invoice_id}/download",
            params={"inline": "false"},
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.content

    async def search_clients(self, query):
        result = await self.get(
            "/clients",
            params={"filter": query, "per_page": 100}
        )
        return result.get("data", [])

    async def search_products(self, query):
        return await self.get(
            "/products",
            params={"filter": query, "per_page": 20}
        )

    async def close(self):
        await self.client.aclose()