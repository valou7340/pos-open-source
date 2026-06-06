"""
services.py — OrderService, SalesService, TicketService, PrinterService
"""
import json, os, socket, threading, datetime
from typing import Dict, List, Optional
from models import Order, OrderItem, _utc_to_local

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ── Helpers JSON ──────────────────────────────────────────────────────────────

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_menu() -> dict:
    return load_json(os.path.join(BASE_DIR, "data", "menu.json"), {})

def load_app_config() -> dict:
    default = {
        "zones": [{"name": "Salle", "count": 10}],
        "restaurant_name": "Mon Restaurant",
        "restaurant_address": "",
        "restaurant_city": "",
        "restaurant_siret": "",
        "restaurant_phone": "",
        "ticket_message": "Merci pour votre visite !",
        "auto_print_kitchen": True,
    }
    cfg = load_json(os.path.join(BASE_DIR, "data", "app_config.json"), default)
    # migration: ancien format table_count → zones
    if "table_count" in cfg and "zones" not in cfg:
        cfg["zones"] = [{"name": "Salle", "count": cfg["table_count"]}]
    cfg.setdefault("zones", default["zones"])
    return cfg

def load_printer_config() -> dict:
    default = {
        "receipt_printer": {"enabled": False, "ip": "192.168.1.100", "port": 9100, "timeout": 5},
        "kitchen_printer": {"enabled": False, "ip": "192.168.1.101", "port": 9100, "timeout": 5},
        "bar_printer":     {"enabled": False, "ip": "192.168.1.102", "port": 9100, "timeout": 5},
    }
    return load_json(os.path.join(BASE_DIR, "data", "printer_config.json"), default)


# ── OrderService ──────────────────────────────────────────────────────────────

class OrderService:
    def __init__(self):
        self._orders: Dict[str, Order] = {}
        self._cfg = load_app_config()

    def get_tables(self) -> List[str]:
        tables = []
        for zone in self._cfg.get("zones", [{"name": "Salle", "count": 10}]):
            name = zone.get("name", "Zone")
            count = zone.get("count", 0)
            for i in range(1, count + 1):
                tables.append(f"{name} {i}")
        tables += ["À emporter", "Comptoir"]
        return tables

    def get_order(self, table: str) -> Order:
        if table not in self._orders:
            self._orders[table] = Order(table=table)
        return self._orders[table]

    def add_item(self, table: str, name: str, price: float, tva_rate: float, category: str) -> Order:
        self.get_order(table).add_item(OrderItem(name=name, price=price, tva_rate=tva_rate, category=category))
        return self.get_order(table)

    def update_qty(self, table: str, name: str, delta: int) -> Order:
        self.get_order(table).update_quantity(name, delta)
        return self.get_order(table)

    def remove_item(self, table: str, name: str) -> Order:
        self.get_order(table).remove_item(name)
        return self.get_order(table)

    def clear(self, table: str) -> None:
        self._orders[table] = Order(table=table)

    def get_occupied(self) -> List[dict]:
        result = []
        for name, order in self._orders.items():
            if not order.items:
                continue
            result.append({
                "table": name,
                "nb_items": sum(i.quantity for i in order.items),
                "total": order.total,
                "status": "Payée" if order.is_paid else "En cours",
                "opened_at": order.created_at.isoformat(),
            })
        return sorted(result, key=lambda x: x["table"])

    def reload_config(self):
        self._cfg = load_app_config()

    def split_equal(self, table: str, n: int) -> List[Order]:
        order = self.get_order(table)
        parts = [Order(table=f"{table} - Part {idx+1}") for idx in range(n)]
        for item in order.items:
            base = item.quantity // n
            remainder = item.quantity % n
            for idx in range(n):
                q = base + (1 if idx < remainder else 0)
                if q > 0:
                    parts[idx].add_item(OrderItem(
                        name=item.name, price=item.price, quantity=q,
                        tva_rate=item.tva_rate, category=item.category))
        return parts

    def apply_split(self, table: str, parts: List[Order]) -> None:
        for i, o in enumerate(parts):
            key = f"{table}-Split{i+1}"
            o.table = key
            self._orders[key] = o
        self.clear(table)


# ── SalesService ──────────────────────────────────────────────────────────────

def _empty_day(date_str: str) -> dict:
    return {
        "date": date_str, "total_ventes_ht": 0.0, "total_ventes_ttc": 0.0,
        "total_tva": 0.0, "ventes_par_taux": {}, "nombre_transactions": 0,
        "ventes_par_moyen_paiement": {}, "transactions": [],
    }

class SalesService:
    def __init__(self):
        self._daily = self._load()

    def _path(self) -> str:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        return os.path.join(BASE_DIR, f"ventes_jour_{today}.json")

    def _load(self) -> dict:
        p = self._path()
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return _empty_day(today)

    def _save(self) -> None:
        save_json(self._path(), self._daily)

    def _check_day_rollover(self) -> None:
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if self._daily.get("date") != today:
            self._save()  # flush le jour précédent avant rollover
            self._daily = self._load()

    def record_order(self, order: Order) -> None:
        self._check_day_rollover()
        self._daily["nombre_transactions"] += 1
        tva_s = order.tva_summary
        total_ht = sum(d["ht"] for d in tva_s.values())
        self._daily["total_ventes_ht"]  += total_ht
        self._daily["total_ventes_ttc"] += order.total
        self._daily["total_tva"]        += (order.total - total_ht)
        for taux, d in tva_s.items():
            key = str(taux)
            if key not in self._daily["ventes_par_taux"]:
                self._daily["ventes_par_taux"][key] = {"ht": 0.0, "tva": 0.0, "ttc": 0.0}
            self._daily["ventes_par_taux"][key]["ht"]  += d["ht"]
            self._daily["ventes_par_taux"][key]["tva"] += d["tva"]
            self._daily["ventes_par_taux"][key]["ttc"] += d["ttc"]
        payments = order.payments if order.payments else [{"method": order.payment_method, "amount": order.total}]
        for p in payments:
            m = p.get("method", "")
            self._daily["ventes_par_moyen_paiement"][m] = \
                self._daily["ventes_par_moyen_paiement"].get(m, 0.0) + p.get("amount", 0.0)
        self._daily["transactions"].append({
            "heure": datetime.datetime.now().strftime("%H:%M:%S"),
            "table": order.table, "montant": order.total,
            "moyen_paiement": order.payment_method,
            "payments": payments,
        })
        self._save()

    def save_to_monthly_file(self, order: Order) -> None:
        now = datetime.datetime.now()
        months_fr = ["Janvier","Fevrier","Mars","Avril","Mai","Juin",
                     "Juillet","Aout","Septembre","Octobre","Novembre","Decembre"]
        folder = os.path.join(BASE_DIR, f"Vente {months_fr[now.month-1]} {now.year}")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "vente.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(order.to_dict(), ensure_ascii=False) + "\n")

    def summary(self) -> dict:
        return self._daily.copy()

    def generate_z_report(self) -> dict:
        now = datetime.datetime.now()
        report = {"type": "RAPPORT_Z",
                  "date_emission": now.strftime("%Y-%m-%d %H:%M:%S"),
                  "date_comptable": now.strftime("%Y-%m-%d"),
                  "numero_rapport": self._next_report_number(),
                  **self._daily}
        d = os.path.join(BASE_DIR, "rapports_z")
        os.makedirs(d, exist_ok=True)
        fname = f"rapport_z_{report['numero_rapport']:04d}_{report['date_comptable']}.json"
        save_json(os.path.join(d, fname), report)
        today = now.strftime("%Y-%m-%d")
        self._daily = _empty_day(today)
        self._save()
        return report

    def _next_report_number(self) -> int:
        p = os.path.join(BASE_DIR, "dernier_rapport.txt")
        try:
            with open(p, "r") as f:
                n = int(f.read().strip())
        except Exception:
            n = 0
        n += 1
        with open(p, "w") as f:
            f.write(str(n))
        return n

    def list_z_reports(self) -> List[dict]:
        d = os.path.join(BASE_DIR, "rapports_z")
        if not os.path.exists(d):
            return []
        result = []
        for fname in sorted(os.listdir(d), reverse=True):
            if fname.endswith(".json"):
                try:
                    data = load_json(os.path.join(d, fname), {})
                    result.append({"filename": fname, "numero": data.get("numero_rapport"),
                                   "date": data.get("date_comptable"),
                                   "ttc": data.get("total_ventes_ttc", 0)})
                except Exception:
                    pass
        return result


# ── TicketService ─────────────────────────────────────────────────────────────

class TicketService:
    def __init__(self):
        self._counter_file = os.path.join(BASE_DIR, "data", "ticket_counter.json")
        self._printed_file = os.path.join(BASE_DIR, "data", "printed_tickets.json")
        os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
        self._cache: dict = load_json(self._printed_file, {})

    def next_number(self) -> int:
        try:
            data = load_json(self._counter_file, {"last_ticket_number": 0})
            n = data.get("last_ticket_number", 0) + 1
            save_json(self._counter_file, {"last_ticket_number": n})
            return n
        except Exception:
            return int(datetime.datetime.now().strftime("%H%M%S"))

    def reset(self) -> None:
        save_json(self._counter_file, {"last_ticket_number": 0})
        save_json(self._printed_file, {})
        self._cache = {}

    @staticmethod
    def order_id(order: Order) -> str:
        return f"{order.table}_{order.created_at.isoformat()}"

    def is_printed(self, oid: str) -> bool:
        return oid in self._cache

    def get_info(self, oid: str) -> Optional[dict]:
        return self._cache.get(oid)

    def mark_printed(self, oid: str, num: int) -> None:
        self._cache[oid] = {"ticket_number": num,
                             "printed_at": datetime.datetime.now().isoformat()}
        save_json(self._printed_file, self._cache)


# ── PrinterService ────────────────────────────────────────────────────────────

class PrinterService:
    def __init__(self, printer_config: dict, app_config: dict):
        self._p = printer_config
        self._a = app_config

    def _send_async(self, cfg: dict, data: str, on_error=None) -> None:
        def _run():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(cfg.get("timeout", 5))
                    s.connect((cfg["ip"], cfg["port"]))
                    s.sendall(data.encode("utf-8"))
            except Exception as e:
                print(f"[Printer] {e}")
                if on_error: on_error(str(e))
        threading.Thread(target=_run, daemon=True).start()

    def print_receipt(self, order: Order, num: int, is_dup=False, on_error=None) -> None:
        cfg = self._p.get("receipt_printer", {})
        if not cfg.get("enabled"): return
        self._send_async(cfg, self._receipt_content(order, num, is_dup), on_error)

    def print_kitchen(self, order: Order, num: int, on_error=None) -> None:
        food   = [i for i in order.items if i.category == "alimentation"]
        drinks = [i for i in order.items if i.category in ("alcool", "boisson sans alcool", "Bieres", "emporte")]
        k = self._p.get("kitchen_printer", {})
        if food and k.get("enabled"):
            self._send_async(k, self._prep_content(order, food, "CUISINE", num), on_error)
        b = self._p.get("bar_printer", {})
        if drinks and b.get("enabled"):
            self._send_async(b, self._prep_content(order, drinks, "BAR", num), on_error)

    def print_z_report(self, report: dict, on_error=None) -> None:
        cfg = self._p.get("receipt_printer", {})
        if not cfg.get("enabled"): return
        self._send_async(cfg, self._z_content(report), on_error)

    def _receipt_content(self, order, num, is_dup) -> str:
        a = self._a
        SEP = "-----------------------------\n"
        name = a.get("restaurant_name", "")
        L = ["\x1B\x40", "\x1B\x61\x01", "\x1B\x21\x30",
             f"{name}\n" if name else "", "\x1B\x21\x00"]
        addr = a.get("restaurant_address", "")
        city = a.get("restaurant_city", "")
        siret = a.get("restaurant_siret", "")
        if addr: L.append(f"{addr}\n")
        if city: L.append(f"{city}\n")
        if siret: L.append(f"Siret: {siret}\n")
        L += [SEP, f"TICKET N°: {num:06d}\n"]
        if is_dup: L += ["\x1B\x21\x10", "*** DUPLICATA ***\n", "\x1B\x21\x00"]
        local_dt = _utc_to_local(order.created_at)
        L += [f"Table: {order.table}\n",
              f"Date: {local_dt.strftime('%d/%m/%Y %H:%M')}\n", SEP,
              "\x1B\x61\x00", "\x1B\x21\x10", "ARTICLE          QTE   PRIX\n",
              "\x1B\x21\x00", SEP]
        for item in order.items:
            L.append(f"{item.name[:16]:<16} {item.quantity:>3} {item.total:>6.2f}EUR\n")
        L += [SEP, f"TOTAL: {order.total:.2f}EUR\n", SEP]
        for rate, d in order.tva_summary.items():
            L.append(f"TVA {rate}%: {d['tva']:.2f}EUR\n")
        msg = a.get("ticket_message", "Merci !")
        L += [SEP, "\x1B\x61\x01", f"{msg}\n", f"Ticket: {num:06d}\n"]
        if is_dup: L += ["\n", "\x1B\x21\x10", "*** DUPLICATA ***\n", "\x1B\x21\x00"]
        L += ["\n\n\n", "\x1D\x56\x00"]
        return "".join(L)

    def _prep_content(self, order, items, dest, num) -> str:
        SEP = "-----------------------------\n"
        L = ["\x1B\x40", "\x1B\x61\x01", "\x1B\x21\x30", f"{dest}\n", "\x1B\x21\x00", SEP,
             f"TICKET N°: {num:06d}\n", f"Table: {order.table}\n",
             f"Heure: {_utc_to_local(order.created_at).strftime('%H:%M')}\n", SEP,
             "\x1B\x61\x00", "\x1B\x21\x08"]
        for item in items:
            L.append(f"{item.name[:16]:<16} {item.quantity:>3}\n")
        L += ["\x1B\x21\x00", SEP, "\n\n\n\n", "\x1D\x56\x00"]
        return "".join(L)

    def _z_content(self, report) -> str:
        SEP = "-----------------------------\n"
        name = self._a.get("restaurant_name", "")
        L = ["\x1B\x40", "\x1B\x61\x01", "\x1B\x21\x30", "RAPPORT Z\n", "\x1B\x21\x00"]
        if name: L.append(f"{name}\n")
        L += [SEP, f"N°: {report['numero_rapport']:04d}\n",
              f"Date: {report['date_emission']}\n", SEP,
              f"HT:  {report['total_ventes_ht']:10.2f}EUR\n",
              f"TVA: {report['total_tva']:10.2f}EUR\n",
              f"TTC: {report['total_ventes_ttc']:10.2f}EUR\n",
              f"Tx:  {report['nombre_transactions']:4d}\n", SEP]
        for taux, d in report.get("ventes_par_taux", {}).items():
            L.append(f"TVA {taux}%: {d['ttc']:8.2f}EUR\n")
        L.append(SEP + "PAIEMENTS\n")
        for moyen, montant in report.get("ventes_par_moyen_paiement", {}).items():
            L.append(f"{moyen:<15} {montant:8.2f}EUR\n")
        L += [SEP, "\x1B\x61\x01", "*** RAPPORT Z ***\n\n\n", "\x1D\x56\x00"]
        return "".join(L)
