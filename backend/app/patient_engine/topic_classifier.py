"""Deterministic keyword-based topic classification of student questions."""
import re

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "greeting": (
        "hello", "hi ", "hi,", "hi!", "hey", "good morning", "good afternoon",
        "nice to meet", "how are you", "how's it going", "how is it going",
    ),
    "condition": (
        "diagnos", "condition", "leukemia", "cancer", "arthritis", "lupus", "sle", "jia",
        "illness", "disease", "treatment", "chemo", "radiation", "mastectomy", "health problem",
        "brought you in", "brings you in", "bring you in", "reason for your visit",
        "why are you here", "here today", "main concern", "biggest concern",
    ),
    "symptoms_pain": (
        "pain", "hurt", "ache", "sore", "stiff", "swell", "swollen", "tingl", "numb", "symptom",
        "fatigue", "tired", "weak", "rash", "skin", "morning",
        "wrist", "elbow", "ankle", "knee", "shoulder", "joint", "hand", "hands", "foot", "feet",
    ),
    "medications": ("medication", "medicine", "med ", "meds", "drug", "prescri", "pill", "side effect"),
    "function_mobility": (
        "walk", "run", "climb", "stairs", "dress", "grip", "grasp", "carry", "lift",
        "reach", "balance", "mobility", "daily task", "daily life", "daily activities",
        "function", "chores", "backpack", "write", "pencil", "get around",
    ),
    "activity_exercise": (
        "exercise", "active", "activity", "sport", "play", "dance", "basketball", "running",
        "workout", "gym", "ymca", "strength train", "outside",
    ),
    "home_environment": ("home", "house", "live", "living", "bedroom", "apartment", "duplex", "mobile home"),
    "family_social": (
        "family", "mom", "mother", "dad", "father", "brother", "sister", "husband", "wife",
        "kids", "children", "son", "daughter", "friend", "social", "support", "grandma", "grandmother", "pet",
    ),
    "school_work": ("school", "work", "job", "class", "teacher", "homeschool", "occupation", "career", "volunteer"),
    "sleep": ("sleep", "nap", "rest", "bedtime", "insomnia", "wake up"),
    "nutrition": ("eat", "food", "diet", "nutrition", "appetite", "drink", "water", "fruit", "vegetable", "meal"),
    "emotional_wellbeing": (
        "feel", "feeling", "mood", "stress", "worry", "worried", "anxious", "sad", "cope",
        "coping", "emotional", "mental", "afraid", "scared", "frustrat", "isolat",
    ),
    "goals_motivation": ("goal", "hope", "want to", "wish", "future", "get back to", "aim", "motivat", "important to you"),
    "healthcare_access": ("insurance", "afford", "cost", "doctor", "physician", "provider", "appointment", "access", "transport"),
    "exam_findings": ("test", "measure", "vital", "blood pressure", "heart rate", "bmi", "screen", "assessment result"),
    "wellness_profile": ("wellness", "paving", "self-care", "lifestyle", "habit", "routine"),
}

# Short, referential questions ("how does that affect you?") that need the
# previous topic to make sense.
_FOLLOW_UP_MARKERS = (
    " that ", " this ", " it ", " those ", " them ",
    "what about", "and then", "after that", "how so", "why is that", "tell me more",
)


def classify(question: str) -> list[str]:
    """Return the matched topics for a student question (ordered, deduplicated)."""
    text = f" {question.lower().strip()} "
    text = re.sub(r"\s+", " ", text)
    matched: list[str] = []
    for topic, keywords in _KEYWORDS.items():
        if any(kw in text for kw in keywords):
            matched.append(topic)
    if not matched:
        matched.append("other")
    return matched


def is_follow_up(question: str) -> bool:
    """Heuristic: the question refers back to something said before."""
    text = f" {question.lower().strip()} "
    text = re.sub(r"\s+", " ", text)
    return any(marker in text for marker in _FOLLOW_UP_MARKERS)
