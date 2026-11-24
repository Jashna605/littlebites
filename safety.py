"""Ingredient text clues. Absence labels never certify dietary or allergy safety."""

import re

ALLERGEN_TERMS = {
    "milk": ("milk", "butter", "cheese", "feta", "yogurt", "yoghurt", "cream", "ghee", "whey", "casein", "buttermilk", "kefir", "curds"),
    "egg": ("egg", "eggs", "mayonnaise", "mayo"),
    "wheat": ("wheat", "wheat flour", "bread", "pasta", "couscous", "bulgur", "semolina", "durum", "orzo", "graham cracker", "breadcrumbs", "bread crumbs", "soy sauce"),
    "peanut": ("peanut", "groundnut"),
    "tree nuts": ("almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nut", "nut butter"),
    "soy": ("soy", "tofu", "edamame", "miso", "tempeh"),
    "fish": ("fish", "salmon", "tuna", "cod", "anchovy", "sardine"),
    "shellfish": ("shrimp", "prawn", "crab", "lobster", "shellfish", "mussel", "oyster"),
    "sesame": ("sesame", "tahini"),
    "coconut": ("coconut",),
}

HIDDEN_SOURCES = {
    "soy sauce": ("soy", "wheat"), "tamari": ("soy",),
    "worcestershire sauce": ("fish",), "worcestershire": ("fish",),
    "fish sauce": ("fish",), "oyster sauce": ("shellfish", "soy", "wheat"),
    "teriyaki sauce": ("soy", "wheat"), "hoisin sauce": ("soy", "wheat"),
    "ranch seasoning": ("milk", "wheat"), "ranch dressing mix": ("milk", "wheat"),
    "graham cracker": ("milk", "wheat"),
}
ALT_MILK_TERMS = (
    "almond milk", "oat milk", "soy milk", "coconut milk", "rice milk", "coconut cream", "oat cream", "soy cream",
    "cream of tartar", "peanut butter", "almond butter", "cashew butter", "sunflower seed butter", "seed butter",
)
ANIMAL_TERMS = (
    "milk", "butter", "cheese", "yogurt", "yoghurt", "cream", "ghee", "whey", "casein", "egg", "eggs", "honey",
    "beef", "chicken", "turkey", "pork", "bacon", "ham", "fish", "salmon", "tuna", "shrimp", "prawn", "crab",
    "lobster", "gelatin", "anchovy", "sardine", "cod", "oyster", "mussel",
)
MEAT_FISH_TERMS = tuple(term for term in ANIMAL_TERMS if term not in {"milk", "butter", "cheese", "yogurt", "yoghurt", "cream", "ghee", "whey", "casein", "egg", "eggs", "honey", "gelatin"})
GLUTEN_TERMS = ("wheat", "wheat flour", "barley", "rye", "malt", "graham cracker", "couscous", "bulgur", "semolina", "durum", "orzo", "pasta", "bread", "soy sauce", "teriyaki sauce", "hoisin sauce")


def _pattern(term):
    words = [re.escape(word) for word in term.split()]
    phrase = r"\s+".join(words)
    last = term.split()[-1]
    if not last.endswith("s"):
        plural = r"(?:s|es)?" if last.endswith(("ch", "sh", "x", "z")) else r"s?"
        phrase = phrase[: -len(words[-1])] + words[-1] + plural
    return re.compile(rf"(?<![a-z0-9]){phrase}(?![a-z0-9])", re.IGNORECASE)


def _matches(terms, text):
    return any(_pattern(term).search(text) for term in terms)


def _without_negated_allergens(text):
    negations = ("peanut", "groundnut", "tree nuts?", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nuts?", "milk", "dairy", "egg", "wheat", "gluten", "soy", "fish", "shellfish", "sesame", "coconut")
    for term in negations:
        text = re.sub(rf"\b{term}(?:[-\s]+free|[-\s]+free-from)\b", " ", text, flags=re.I)
        text = re.sub(rf"\bfree\s+(?:from|of)\s+{term}\b", " ", text, flags=re.I)
    return text


def tag(ingredients):
    text = _without_negated_allergens(" ".join(str(item) for item in ingredients).casefold())
    milk_text = text
    for term in ALT_MILK_TERMS:
        milk_text = _pattern(term).sub(" ", milk_text)
    found = set()
    for group, terms in ALLERGEN_TERMS.items():
        source = milk_text if group == "milk" else text
        if _matches(terms, source):
            found.add(group)
    for source, warnings in HIDDEN_SOURCES.items():
        if _pattern(source).search(text):
            found.update(warnings)

    tags = [name for name in ALLERGEN_TERMS if name in found]
    if _matches(("honey",), text):
        tags.append("honey: not safe under 12 months")
    has_oats = _matches(("oat",), text)
    has_gluten = _matches(GLUTEN_TERMS, text)
    if has_oats:
        tags.append("may contain gluten")
    if not has_gluten and not has_oats:
        tags.append("no listed gluten found")

    has_animal = _matches(ANIMAL_TERMS, milk_text)
    has_meat_fish = _matches(MEAT_FISH_TERMS, milk_text)
    if not has_meat_fish:
        tags.append("no listed meat or fish found")
    if not has_animal:
        tags.append("no listed animal ingredients found")
    if "milk" not in found:
        tags.append("no listed dairy found")
    return list(dict.fromkeys(tags))
