import re

STOPWORDS = {'inc','corp','corporation','company','co','ltd','llc','plc','holdings','group','the','technologies','technology','systems'}
ALIASES = {
    'international business machines': ['ibm'],
    'alphabet': ['google'],
    'meta platforms': ['facebook', 'meta'],
    'amazon com': ['amazon'],
    'apple': ['apple computer'],
}

def normalize_name(name: str) -> str:
    if not name:
        return ''
    s = re.sub(r'[^a-z0-9 ]+', ' ', name.lower())
    tokens = [t for t in s.split() if t and t not in STOPWORDS]
    return ' '.join(tokens)

def expand_aliases(name: str):
    n = normalize_name(name)
    vals = {n}
    for canonical, aliases in ALIASES.items():
        can = normalize_name(canonical)
        if n == can or n in [normalize_name(a) for a in aliases]:
            vals.add(can)
            for a in aliases:
                vals.add(normalize_name(a))
    return vals

def names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    set_a = expand_aliases(a)
    set_b = expand_aliases(b)
    if set_a & set_b:
        return True
    for na in set_a:
        for nb in set_b:
            if na == nb or na in nb or nb in na:
                return True
    return False
