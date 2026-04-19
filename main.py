"""
main.py — FastAPI POS v4
Accessible via navigateur web depuis n'importe quel appareil sur le réseau local.

Lancement : python start.py  (ou uvicorn main:app --host 0.0.0.0 --port 8000)
"""
import json
import asyncio
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from services import (OrderService, SalesService, TicketService, PrinterService,
                      load_menu, load_app_config, load_printer_config, load_json,
                      save_json, BASE_DIR, os)

app = FastAPI(title="POS v4")

# ── Singletons ────────────────────────────────────────────────────────────────
order_svc   = OrderService()
sales_svc   = SalesService()
ticket_svc  = TicketService()
printer_svc = PrinterService(load_printer_config(), load_app_config())

# ── WebSocket broadcast ───────────────────────────────────────────────────────
_ws_clients: Set[WebSocket] = set()

async def _broadcast(msg: dict):
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)

async def _broadcast_tables():
    await _broadcast({"type": "tables_update", "occupied": order_svc.get_occupied()})

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(ws)

# ── Static / HTML ─────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/api/menu")
async def get_menu():
    return load_menu()

@app.get("/api/config")
async def get_config():
    cfg = load_app_config()
    cfg.setdefault("table_count", 20)
    return cfg

@app.get("/api/printer_config")
async def get_printer_config():
    return load_printer_config()

@app.get("/api/layout")
async def get_layout():
    path = os.path.join(BASE_DIR, "data", "layout.json")
    return load_json(path, {})

class LayoutSavePayload(BaseModel):
    layout: dict

@app.post("/api/layout/save")
async def save_layout(payload: LayoutSavePayload):
    path = os.path.join(BASE_DIR, "data", "layout.json")
    save_json(path, payload.layout)
    return {"ok": True}

@app.get("/api/tables")
async def get_tables():
    return order_svc.get_tables()

@app.get("/api/occupied")
async def get_occupied():
    return order_svc.get_occupied()

@app.get("/api/order/{table}")
async def get_order(table: str):
    return order_svc.get_order(table).to_dict()


class ItemPayload(BaseModel):
    name: str
    price: float
    tva_rate: float = 10.0
    category: str = "alimentation"

@app.post("/api/order/{table}/add")
async def add_item(table: str, payload: ItemPayload):
    order = order_svc.add_item(table, payload.name, payload.price,
                                payload.tva_rate, payload.category)
    await _broadcast({"type": "order_update", "table": table, "order": order.to_dict()})
    await _broadcast_tables()
    return order.to_dict()


class QtyPayload(BaseModel):
    name: str
    delta: int

@app.post("/api/order/{table}/qty")
async def update_qty(table: str, payload: QtyPayload):
    order = order_svc.update_qty(table, payload.name, payload.delta)
    await _broadcast({"type": "order_update", "table": table, "order": order.to_dict()})
    return order.to_dict()


class RemovePayload(BaseModel):
    name: str

@app.post("/api/order/{table}/remove")
async def remove_item(table: str, payload: RemovePayload):
    order = order_svc.remove_item(table, payload.name)
    await _broadcast({"type": "order_update", "table": table, "order": order.to_dict()})
    return order.to_dict()

@app.delete("/api/order/{table}")
async def clear_order(table: str):
    order_svc.clear(table)
    await _broadcast({"type": "order_update", "table": table,
                      "order": order_svc.get_order(table).to_dict()})
    await _broadcast_tables()
    return {"ok": True}


class PaymentPayload(BaseModel):
    method: str = ""
    payments: list = []

@app.post("/api/order/{table}/pay")
async def pay(table: str, payload: PaymentPayload):
    order = order_svc.get_order(table)
    if not order.items:
        raise HTTPException(400, "Commande vide")
    payments = payload.payments if payload.payments else [{"method": payload.method, "amount": order.total}]
    paid_total = round(sum(p["amount"] for p in payments), 2)
    if paid_total < order.total:
        raise HTTPException(400, f"Montant insuffisant ({paid_total:.2f} < {order.total:.2f})")
    order.payments = payments
    order.payment_method = " + ".join(p["method"] for p in payments)
    order.is_paid = True
    sales_svc.record_order(order)
    sales_svc.save_to_monthly_file(order)
    order_svc.clear(table)
    await _broadcast({"type": "order_update", "table": table,
                      "order": order_svc.get_order(table).to_dict()})
    await _broadcast({"type": "payment_done", "table": table,
                      "method": order.payment_method, "total": order.total})
    await _broadcast_tables()
    return {"ok": True, "total": order.total}


class PrintPayload(BaseModel):
    target: str  # "client" | "cuisine" | "both"

@app.post("/api/order/{table}/print")
async def print_ticket(table: str, payload: PrintPayload):
    order = order_svc.get_order(table)
    if not order.items:
        raise HTTPException(400, "Commande vide")
    oid    = ticket_svc.order_id(order)
    is_dup = ticket_svc.is_printed(oid)
    if is_dup:
        info = ticket_svc.get_info(oid)
        num  = info["ticket_number"] if info else ticket_svc.next_number()
    else:
        num = ticket_svc.next_number()
    if payload.target in ("client", "both"):
        printer_svc.print_receipt(order, num, is_dup)
    if payload.target in ("cuisine", "both") and not is_dup:
        printer_svc.print_kitchen(order, num)
    if not is_dup:
        ticket_svc.mark_printed(oid, num)
    return {"ok": True, "ticket_number": num, "duplicate": is_dup}


@app.get("/api/sales/today")
async def sales_today():
    return sales_svc.summary()

@app.post("/api/sales/z_report")
async def z_report():
    report = sales_svc.generate_z_report()
    printer_svc.print_z_report(report)
    return report

@app.get("/api/sales/z_reports")
async def list_z_reports():
    return sales_svc.list_z_reports()

@app.post("/api/tickets/reset")
async def reset_tickets():
    ticket_svc.reset()
    return {"ok": True}


class ConfigSavePayload(BaseModel):
    config: dict

@app.post("/api/config/save")
async def save_config(payload: ConfigSavePayload):
    path = os.path.join(BASE_DIR, "data", "app_config.json")
    save_json(path, payload.config)
    order_svc.reload_config()
    global printer_svc
    printer_svc = PrinterService(load_printer_config(), payload.config)
    return {"ok": True}


class PrinterConfigPayload(BaseModel):
    config: dict

@app.post("/api/config/printer/save")
async def save_printer_config(payload: PrinterConfigPayload):
    path = os.path.join(BASE_DIR, "data", "printer_config.json")
    save_json(path, payload.config)
    global printer_svc
    printer_svc = PrinterService(payload.config, load_app_config())
    return {"ok": True}


class MenuSavePayload(BaseModel):
    menu: dict

@app.post("/api/menu/save")
async def save_menu(payload: MenuSavePayload):
    path = os.path.join(BASE_DIR, "data", "menu.json")
    save_json(path, payload.menu)
    return {"ok": True}


class SplitPayload(BaseModel):
    n: int

@app.post("/api/order/{table}/split")
async def split_order(table: str, payload: SplitPayload):
    parts = order_svc.split_equal(table, payload.n)
    order_svc.apply_split(table, parts)
    await _broadcast({"type": "split_done", "table": table})
    return {"ok": True, "parts": [p.to_dict() for p in order_svc.get_occupied()]}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
