"""Pure parsers for product food databases (ROADMAP P12).

Vendor payload in, :class:`FoodItem` out. No I/O, so the same product always
yields the same item and a logged entry can be re-derived from its raw payload
like everything else (§2.1).

**Everything is per 100 g.** Food databases quote nutrition per 100 g and then
separately describe a serving; keeping the canonical figures on that one basis
means the portion arithmetic happens in exactly one place
(:func:`scale_to_grams`) instead of being re-derived per source.

**Absence is absence** (§2.7), and it matters more here than almost anywhere
else in the codebase. A crowd-sourced product with no fibre value has *unknown*
fibre, not zero — and a coach that treats unknown macros as zeros will quietly
report a day's intake as lower than it was, which is the exact direction the
user is already biased in (risk #7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FoodItem:
    """One product, normalized. All nutrition is per 100 g."""

    source: str  # 'openfoodfacts' | 'usda' | ...
    external_id: str
    name: str
    brand: str | None
    kcal_per_100g: float | None
    protein_g_per_100g: float | None
    carbs_g_per_100g: float | None
    fat_g_per_100g: float | None
    fiber_g_per_100g: float | None
    # The label's own serving, when it states one. Lets the CLI offer "1 serving"
    # without inventing a portion size.
    serving_g: float | None
    serving_label: str | None

    @property
    def has_any_nutrition(self) -> bool:
        return any(
            v is not None
            for v in (
                self.kcal_per_100g,
                self.protein_g_per_100g,
                self.carbs_g_per_100g,
                self.fat_g_per_100g,
            )
        )

    @property
    def display(self) -> str:
        return f"{self.name} — {self.brand}" if self.brand else self.name


def _num(value: Any) -> float | None:
    """A number, or None. Never a default.

    Open Food Facts is crowd-sourced: fields arrive as strings, as empty
    strings, and occasionally as nonsense. Anything unparseable is *unknown*.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # A negative or absurd energy figure is a data-entry error, not a food.
    return out if out >= 0 else None


def parse_openfoodfacts(product: dict[str, Any]) -> FoodItem | None:
    """Open Food Facts product -> :class:`FoodItem`, or None if unusable.

    Returns None when the product has no name or no nutrition at all. A row that
    logs "something, unknown calories" adds nothing the coach can use and would
    show up in a day's totals as a phantom entry.
    """
    code = str(product.get("code") or "").strip()
    name = str(product.get("product_name") or "").strip()
    if not code or not name:
        return None

    n = product.get("nutriments") or {}
    # OFF exposes energy in both kJ and kcal; prefer the kcal field rather than
    # converting, so we report the number the label actually carries.
    kcal = _num(n.get("energy-kcal_100g"))
    if kcal is None:
        kj = _num(n.get("energy_100g"))
        kcal = kj / 4.184 if kj is not None else None

    brands = str(product.get("brands") or "").strip()
    item = FoodItem(
        source="openfoodfacts",
        external_id=code,
        name=name,
        # OFF stores brands as a comma-separated list; the first is the label.
        brand=brands.split(",")[0].strip() or None if brands else None,
        kcal_per_100g=kcal,
        protein_g_per_100g=_num(n.get("proteins_100g")),
        carbs_g_per_100g=_num(n.get("carbohydrates_100g")),
        fat_g_per_100g=_num(n.get("fat_100g")),
        fiber_g_per_100g=_num(n.get("fiber_100g")),
        serving_g=_num(product.get("serving_quantity")),
        serving_label=str(product.get("serving_size") or "").strip() or None,
    )
    return item if item.has_any_nutrition else None


@dataclass(frozen=True)
class ScaledPortion:
    """A :class:`FoodItem` at a specific portion size."""

    grams: float
    kcal: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    fiber_g: float | None


def scale_to_grams(item: FoodItem, grams: float) -> ScaledPortion:
    """Scale per-100g nutrition to a portion. The one place this arithmetic lives.

    Unknown stays unknown: a None macro scales to None, never to 0.0. Rounded to
    one decimal because the underlying data is a crowd-sourced label, and
    printing 47.3184 g of carbs would imply a precision the source does not have.
    """
    if grams <= 0:
        raise ValueError(f"a portion must be a positive number of grams, got {grams}")
    f = grams / 100.0

    def _s(v: float | None) -> float | None:
        return None if v is None else round(v * f, 1)

    return ScaledPortion(
        grams=grams,
        kcal=_s(item.kcal_per_100g),
        protein_g=_s(item.protein_g_per_100g),
        carbs_g=_s(item.carbs_g_per_100g),
        fat_g=_s(item.fat_g_per_100g),
        fiber_g=_s(item.fiber_g_per_100g),
    )
