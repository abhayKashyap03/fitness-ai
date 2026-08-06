"""Product food databases — the sanctioned nutrition path (ROADMAP P12).

One module per source, each translating into ``normalize.foods.FoodItem`` at the
edge (§2.5). Open Food Facts is here today; USDA FoodData Central slots in
beside it as a new file, not a schema change.

Why this exists at all: the MyFitnessPal adapter is a signed-off override for
the owner's OWN diary (ADR-0009/0010) and explicitly must not ship as a product
feature. These are the sources that can.
"""
