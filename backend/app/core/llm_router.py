from typing import List, Dict, Any

def has_conditional_logic(text: str) -> bool:
    """Detect if the message contains words indicating conditional rules or dependencies."""
    keywords = ["if", "only if", "check first", "depending on", "unless", "conditional","where"]
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)

def select_model(iteration: int, messages: List[Dict[str, Any]]) -> str:
    """
    Selects either 'deepseek-reasoner' or 'deepseek-chat' based on the iteration
    index and the presence of conditional logic in the user's initial prompt.
    """
    if iteration == 0:
        # Retrieve the latest user message
        user_messages = [msg for msg in messages if msg.get("role") == "user"]
        if user_messages:
            last_user_content = user_messages[-1].get("content", "")
            if isinstance(last_user_content, str) and has_conditional_logic(last_user_content):
                return "deepseek-reasoner"
    return "deepseek-chat"
