"""
Constants for the Studio module — shot controls, model library, option lists.
"""
from __future__ import annotations

LIGHTING_PRESETS = [
    "Clean E-commerce",
    "Dramatic",
    "Natural Window",
    "Golden Hour",
    "Neon Glow",
    "Overcast Day",
]

CAMERA_ANGLES = [
    "Eye-Level",
    "Hero (Low Angle)",
    "45-Degree",
    "Top-Down",
    "Dutch Angle",
]

MATERIALS = ["Matte", "Glossy", "Metallic", "Glass"]

SHOT_TYPES = [
    "Full Body Front",
    "3/4 View",
    "Walking Motion",
    "Candid",
    "Hands in Pockets",
    "Elegant Lean",
]

EXPRESSIONS = ["Neutral", "Soft Smile", "Confident", "Joyful", "Serene"]

PRODUCT_INTERACTIONS = ["Holding", "Wearing", "Presenting", "Interacting"]

SURFACES = [
    "Marble Slab",
    "Polished Wood",
    "Concrete",
    "Brushed Metal",
    "Black Slate",
]

BACKGROUNDS = [
    "City Street",
    "Modern Interior",
    "Cozy Cafe",
    "Lush Forest",
    "Sunny Beach",
    "Rooftop Garden",
    "Vintage Room",
    "Desert Dune",
]

COLOR_GRADES = [
    "None",
    "Cinematic Teal & Orange",
    "Vintage Film",
    "Vibrant & Punchy",
    "Muted & Moody",
    "Warm & Golden",
    "Cool & Crisp",
]

TIME_OF_DAY = ["None", "Sunrise", "Midday", "Golden Hour", "Twilight", "Night"]

LAYOUT_STYLES = [
    "Minimal White",
    "Textured Surface",
    "Color-Coordinated",
    "Scattered Organic",
]

LIGHT_QUALITIES = ["Soft", "Hard", "Ring Light"]

SHADOW_OPTIONS = ["Soft", "Hard", "None"]

# Prompt snippets for each color grade (used in auto-prompt assembly)
COLOR_GRADE_PROMPTS: dict[str, str] = {
    "None": "",
    "Cinematic Teal & Orange": "cinematic teal and orange color grade",
    "Vintage Film": "vintage film look, warm faded colors, subtle grain",
    "Vibrant & Punchy": "vibrant high-saturation colors, punchy look",
    "Muted & Moody": "desaturated muted tones, moody atmosphere",
    "Warm & Golden": "warm golden-toned grade, sunny inviting atmosphere",
    "Cool & Crisp": "cool-toned crisp blues and whites, fresh modern feel",
}

# Default shot controls — used when user hasn't changed anything
SHOT_DEFAULTS = {
    "shot1": {
        "camera_angle": "Eye-Level",
        "lighting": "Clean E-commerce",
        "material": "None",
        "hyper_realism": False,
    },
    "shot2": {
        "shot_type": "Full Body Front",
        "expression": "Confident",
        "product_interaction": "Holding",
    },
    "shot3": {
        "scene": "City Street",
        "lighting": "Golden Hour",
        "time_of_day": "None",
    },
    "shot4": {
        "surface": "Marble Slab",
        "camera_angle": "Macro Detail",
        "light_quality": "Soft",
        "shadow": "Soft",
    },
    "shot5": {
        "layout_style": "Minimal White",
        "camera_angle": "Top-Down",
        "color_grade": "None",
    },
}

# 18-model library from the AI creative studio reference
MODEL_LIBRARY = [
    {
        "id": "m1",
        "name": "Sasha",
        "region": "Germany",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/3772510/pexels-photo-3772510.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a professional female fashion model, early 20s, long straight blonde hair, sharp cheekbones, confident gaze, slender athletic build, European ethnicity",
    },
    {
        "id": "m2",
        "name": "Kenji",
        "region": "Japan",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/428340/pexels-photo-428340.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a male streetwear model, mid-20s, short dark curly hair, friendly smile, lean build, cool relaxed demeanor, East Asian ethnicity",
    },
    {
        "id": "m3",
        "name": "Maria",
        "region": "Mexico",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/3778223/pexels-photo-3778223.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a versatile female commercial model, late 20s, warm brown skin, shoulder-length wavy dark hair, expressive brown eyes, average build, warm approachable look, Latina ethnicity",
    },
    {
        "id": "m4",
        "name": "David",
        "region": "USA",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/936043/pexels-photo-936043.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a rugged male model, early 30s, short beard, piercing blue eyes, light brown hair, muscular build, outdoor lifestyle look, Caucasian ethnicity",
    },
    {
        "id": "m5",
        "name": "Priya",
        "region": "India",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/3762800/pexels-photo-3762800.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a graceful female fashion model, mid-20s, long dark wavy hair, expressive almond-shaped brown eyes, warm olive skin, poised and elegant presence, South Asian Indian ethnicity",
    },
    {
        "id": "m6",
        "name": "Chloe",
        "region": "Ireland",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/3220617/pexels-photo-3220617.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "an approachable female model, early 20s, freckles, red curly hair, green eyes, slim build, cheerful friendly vibe, Irish ethnicity",
    },
    {
        "id": "m7",
        "name": "Jamal",
        "region": "USA",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/1853542/pexels-photo-1853542.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a sharp male model, late 20s, short well-groomed dreadlocks, confident expression, strong jawline, athletic build, African-American ethnicity",
    },
    {
        "id": "m8",
        "name": "Isabella",
        "region": "Brazil",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/3755440/pexels-photo-3755440.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a high-fashion female model, early 20s, sleek black hair in a bob cut, sharp angular features, sophisticated look, tall slender frame, Brazilian ethnicity",
    },
    {
        "id": "m9",
        "name": "Omar",
        "region": "Egypt",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/2379005/pexels-photo-2379005.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a mature male model, late 30s, salt-and-pepper hair, well-maintained short beard, kind eyes, distinguished and trustworthy appearance, Middle Eastern ethnicity",
    },
    {
        "id": "m10",
        "name": "Anja",
        "region": "Sweden",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/3775164/pexels-photo-3775164.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a striking female model, mid-20s, platinum blonde short hair, bold eyebrows, androgynous look, lean strong physique, Scandinavian ethnicity",
    },
    {
        "id": "m11",
        "name": "Aisha",
        "region": "India",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/1130626/pexels-photo-1130626.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a vibrant female model from Mumbai, early 20s, expressive dark eyes, long black hair, warm engaging smile, perfect for contemporary Indian fashion, Indian ethnicity",
    },
    {
        "id": "m12",
        "name": "Jackson",
        "region": "USA",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/1212984/pexels-photo-1212984.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a male lifestyle model from California, mid-20s, sun-kissed look, blonde hair, laid-back athletic vibe, Caucasian ethnicity",
    },
    {
        "id": "m13",
        "name": "Amélie",
        "region": "France",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/1382731/pexels-photo-1382731.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "an elegant female model from Paris, late 20s, chic brunette hair, classic features, sophisticated aura, effortless French style, European ethnicity",
    },
    {
        "id": "m14",
        "name": "Li Wei",
        "region": "China",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/2896434/pexels-photo-2896434.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a sharp high-fashion male model from Shanghai, early 20s, striking angular features, modern edgy look, East Asian ethnicity",
    },
    {
        "id": "m15",
        "name": "Sofia",
        "region": "Colombia",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/1848565/pexels-photo-1848565.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a charismatic female model from Colombia, tan skin, voluminous curly brown hair, joyful energetic presence, Latina ethnicity",
    },
    {
        "id": "m16",
        "name": "Khalid",
        "region": "UAE",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/1516680/pexels-photo-1516680.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a luxurious male model from Dubai, early 30s, perfectly groomed beard, intense gaze, polished upscale look, Emirati ethnicity",
    },
    {
        "id": "m17",
        "name": "Adanna",
        "region": "Nigeria",
        "gender": "Female",
        "thumbnail": "https://images.pexels.com/photos/2776353/pexels-photo-2776353.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a stunning female model from Nigeria, rich dark skin, braided hair, powerful regal presence, bold and couture fashion, Nigerian ethnicity",
    },
    {
        "id": "m18",
        "name": "Mike",
        "region": "USA",
        "gender": "Male",
        "thumbnail": "https://images.pexels.com/photos/1680172/pexels-photo-1680172.jpeg?auto=compress&cs=tinysrgb&w=200&h=300&dpr=2",
        "description": "a strong male model from New York, confident attitude, versatile look that fits streetwear and high fashion, African-American ethnicity",
    },
]
