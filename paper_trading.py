
"""Local paper-trading simulator. Never calls a FYERS order endpoint."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

@dataclass
class PaperPosition:
    symbol: str = ""
    side: str = ""
    qty: int = 0
    entry: float = 0.0
    stop_loss: float | None = None
    target: float | None = None
    realized_pnl: float = 0.0
    status: str = "FLAT"
    opened_at: str = ""
    closed_at: str = ""
    exit_reason: str = ""

class PaperTrader:
    def __init__(self):
        self.position = PaperPosition()
        self.history = []

    def open(self, symbol, side, qty, price, sl_points=0.0, target_points=0.0):
        if self.position.qty:
            return False, "A paper position is already open."
        side = side.upper()
        if side not in ("BUY", "SELL"):
            return False, "Side must be BUY or SELL."
        price = float(price); qty = max(1, int(qty))
        sl = price - sl_points if side == "BUY" and sl_points > 0 else price + sl_points if side == "SELL" and sl_points > 0 else None
        tp = price + target_points if side == "BUY" and target_points > 0 else price - target_points if side == "SELL" and target_points > 0 else None
        self.position = PaperPosition(symbol=symbol, side=side, qty=qty, entry=price,
            stop_loss=sl, target=tp, status="OPEN",
            opened_at=datetime.now(timezone.utc).isoformat())
        return True, f"Paper {side} filled @ {price:.2f}"

    def mark(self, price):
        p=self.position
        if not p.qty: return None
        price=float(price); direction=1 if p.side=="BUY" else -1
        pnl=(price-p.entry)*p.qty*direction
        reason=None
        if p.side=="BUY":
            if p.stop_loss is not None and price<=p.stop_loss: reason="SL"
            elif p.target is not None and price>=p.target: reason="TARGET"
        else:
            if p.stop_loss is not None and price>=p.stop_loss: reason="SL"
            elif p.target is not None and price<=p.target: reason="TARGET"
        if reason:
            return self._exit(price,pnl,reason)
        return {"reason":None,"price":price,"pnl":pnl}

    def close(self, price):
        p=self.position
        if not p.qty: return False, "No paper position."
        price=float(price); direction=1 if p.side=="BUY" else -1
        pnl=(price-p.entry)*p.qty*direction
        self._exit(price,pnl,"MANUAL")
        return True,pnl

    def _exit(self,price,pnl,reason):
        p=self.position
        p.realized_pnl += pnl; p.status=f"EXIT {reason}"
        p.exit_reason=reason; p.closed_at=datetime.now(timezone.utc).isoformat()
        self.history.append({"symbol":p.symbol,"side":p.side,"qty":p.qty,"entry":p.entry,
                             "exit":price,"pnl":pnl,"reason":reason,"closed_at":p.closed_at})
        p.qty=0; p.entry=0.0; p.stop_loss=None; p.target=None; p.side=""
        return {"reason":reason,"price":price,"pnl":pnl}

    def snapshot(self, price=None):
        p=self.position; unreal=0.0
        if p.qty and price is not None:
            direction=1 if p.side=="BUY" else -1
            unreal=(float(price)-p.entry)*p.qty*direction
        d=asdict(p); d["unrealized_pnl"]=unreal; d["total_pnl"]=p.realized_pnl+unreal
        d["history"]=list(self.history[-50:])
        return d
