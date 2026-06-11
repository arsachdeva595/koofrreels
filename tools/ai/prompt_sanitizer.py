"""
Prompt sanitizer — scrubs words/phrases that trigger Google Veo3 content
filters before sending to the Vertex AI API.

Replacements preserve sentence flow where possible. The substitution log
returned alongside the cleaned text helps with debugging (logged by veo3_client).
"""
from __future__ import annotations

import re

# (regex_pattern, safe_replacement)
# Applied in order — put longer/more-specific phrases before short words.
_RULES: list[tuple[str, str]] = [

    # ── Self-harm (before "cut" catches these) ────────────────────────────────
    (r'\b(suicide|suicidal)\b',                             'deep thought'),
    (r'\bself[\s\-]?harm\b',                                'personal struggle'),
    (r'\bcutting (?:her|his|their|one\'?s)? ?wrists?\b',    'reflection'),
    (r'\b(overdos(?:e|ing)|hang(?:ing)? (?:(?:him|her|them)self|oneself))\b', 'rest'),

    # ── Weapons & explosives ──────────────────────────────────────────────────
    (r'\b(IED|pipe[\s\-]?bomb)\b',                          'device'),
    (r'\b(bomb[s]?|explosive[s]?|grenade[s]?|landmine[s]?)\b', 'object'),
    (r'\b(firearm[s]?|handgun[s]?|pistol[s]?|rifle[s]?|shotgun[s]?|revolver[s]?)\b', 'object'),
    (r'\b(AK[\-\s]?47|AR[\-\s]?15|machine gun[s]?|assault rifle[s]?)\b', 'object'),
    (r'\b(bullet[s]?|ammunition|ammo|cartridge[s]?)\b',     'element'),
    (r'\b(knife|knives|dagger[s]?|blade[s]?|machete[s]?|cleaver[s]?)\b', 'object'),
    (r'\b(sword[s]?|spear[s]?|axe[s]?|hatchet[s]?)\b',     'object'),
    (r'\b(chemical weapon[s]?|nerve agent[s]?|bioweapon[s]?)\b', 'substance'),
    (r'\b(weapon[s]?|arms?)\b',                             'tool'),

    # ── Violence & gore ───────────────────────────────────────────────────────
    (r'\b(murder(?:ing|ed|er[s]?)?|homicide|manslaughter)\b', 'overcome'),
    (r'\b(kill(?:ing|ed|er[s]?)?|slay(?:ing)?|slaughter(?:ing|ed)?)\b', 'outshine'),
    (r'\b(execut(?:e|ing|ion)|behead(?:ing)?|decapitat(?:e|ing|ion))\b', 'finish'),
    (r'\b(torture(?:ing|ed)?|torment(?:ing|ed)?)\b',        'challenge'),
    (r'\b(gore|gory|graphic(?:ally)? violent)\b',            'intense'),
    (r'\b(blood(?:y|shed|bath)?|hemorrhage)\b',              'vivid'),
    (r'\b(stab(?:bing|bed)?|slash(?:ing|ed)?|pierce(?:ing|d)?(?= (?:through|into|with)))\b', 'cut through'),
    (r'\b(brutali[sz](?:e|ing|ed)?|brutalit[yi])\b',        'intensity'),
    (r'\b(violen(?:t(?:ly)?|ce))\b',                        'energetic'),
    (r'\b(war(?:fare)?|combat|battlefield[s]?)\b',           'journey'),
    (r'\b(attack(?:ing|ed)?|assault(?:ing|ed)?)\b',          'approach'),
    (r'\b(dead|death|dying|corpse[s]?|cadaver[s]?|carcass(?:es)?)\b', 'still'),
    (r'\b(die[s]?|died)\b',                                 'fades'),

    # ── Dangerous activities / drugs ──────────────────────────────────────────
    (r'\b(cocaine|heroin|methamphetamine|meth|crack cocaine|fentanyl|opioid[s]?)\b', 'substance'),
    (r'\b(drug[s]? (?:deal(?:ing|er)|traffick(?:ing|er)))\b', 'activity'),
    (r'\b(ransomware|malware|spyware|phishing|cyberattack|zero[\-\s]?day exploit)\b', 'access'),

    # ── Sexual content ────────────────────────────────────────────────────────
    (r'\b(nude|naked|nudity|topless|bare(?:d)? (?:skin|body|chest))\b', 'expressive'),
    (r'\b(pornograph(?:y|ic)|erotic[a]?|NSFW|explicit (?:sexual|content))\b', 'bold'),
    (r'\b(sexual(?:ly)?|intercourse|genitali[a]?)\b',       'personal'),

    # ── Hate speech indicators ────────────────────────────────────────────────
    (r'\b(racist|bigot(?:ry)?|supremac(?:y|ist)|fascis(?:t|m))\b', 'person'),
    (r'\b(terrorist(?:s)?|terrorism|extremis(?:t|m)|jihad(?:ist)?)\b', 'activist'),
    (r'\b(propaganda|radicali[sz](?:e|ing|ed|ation))\b',    'movement'),
    (r'\b(dehumaniz(?:e|ing|ed|ation)|subhuman[s]?)\b',     'person'),

    # ── Hinglish / transliterated Hindi trigger expressions ───────────────────
    # Common colloquial Hinglish that maps to flagged English concepts.
    (r'\b(maar[\s\-]?daala|maar[\s\-]?diya|maar[\s\-]?di)\b',  'outshone'),
    (r'\b(maar(?:na|te|ti|ta|raha|rahi)?)\b',                   'outshine'),
    (r'\b(marn[ae][\s\-]?(?:wala|wali|wale)?)\b',               'slowing down'),
    (r'\b(mar[\s\-]?(?:gaya|gayi|gaye|jao|jaa))\b',             'faded away'),
    (r'\b(khoon)\b',                                            'essence'),
    (r'\b(laash)\b',                                            'figure'),
    (r'\b(ladai|jhagda|jhagra|fasaad)\b',                       'challenge'),
    (r'\b(danga)\b',                                            'commotion'),
    (r'\b(tabahi|barbaad)\b',                                   'transformed'),
    (r'\b(hatya|qatl)\b',                                       'overcome'),
    (r'\b(nanga|naange)\b',                                     'natural'),
    (r'\b(chaku|churi|khanjar)\b',                              'object'),
    (r'\b(bandook)\b',                                          'tool'),
    (r'\b(dhamaka)\b',                                          'burst of energy'),
    (r'\b(aatankwad[i]?)\b',                                    'movement'),
    (r'\b(nasha|charas|afeem)\b',                               'haze'),

    # ── Commonly flagged intensity words (metaphorical use) ───────────────────
    (r'\b(rant(?:ing|ed)?)\b',                              'speaking passionately'),
    (r'\b(scream(?:ing|ed)? (?:in|with) (?:rage|anger|fury))\b', 'expressing emotion intensely'),
    (r'\b(destroy(?:ing|ed|s)?)\b',                         'transform'),
    (r'\b(crush(?:ing|ed)?)\b',                             'overcome'),
    (r'\b(explod(?:e|ing|ed))\b',                           'burst'),
    (r'\b(burn(?:ing|ed|s)? (?:down|alive|everything))\b',  'transform completely'),
    (r'\b(terrif(?:y|ying|ied)|horrif(?:y|ying|ied)|terror(?:izing)?)\b', 'surprise'),
]

_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, re.IGNORECASE), rep) for pat, rep in _RULES
]


def sanitize_prompt(text: str) -> tuple[str, list[str]]:
    """Return (cleaned_text, list_of_substitutions_made).

    Substitution log is non-empty only when changes were made.
    """
    changes: list[str] = []

    for pattern, replacement in _COMPILED:
        def _replace(m: re.Match, rep: str = replacement) -> str:
            original = m.group()
            # Preserve leading capitalisation
            if original[0].isupper():
                rep = rep[0].upper() + rep[1:]
            changes.append(f"'{original}' → '{rep}'")
            return rep

        text = pattern.sub(_replace, text)

    return text, changes
