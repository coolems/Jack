"""
User-facing status/error messages for llama.cpp provider.
"""
from config import MODEL_NAME, LLAMA_URL
from typing import Optional


def status_message(error: Exception, model_name: Optional[str] = None, timeout: Optional[int] = None) -> str:
    """Generate a user-friendly status message from an error."""
    if model_name is None:
        model_name = MODEL_NAME

    error_str = str(error).lower()

    if "connection" in error_str or "refused" in error_str:
        return (
            "WARNING: llama.cpp Service Not Running\n\n"
            "The AI backend (llama.cpp) is not responding. Please:\n\n"
            "1. Start llama-server with your model\n"
            f"2. Check if running: Visit {LLAMA_URL}/health\n\n"
            "Once llama-server is running, send your message again."
        )

    elif "timeout" in error_str or "timed out" in error_str:
        time_info = f"({timeout} seconds)" if timeout else ""
        return (
            f"WARNING: llama.cpp Response Timeout\n\n"
            f"The AI model took too long to respond {time_info}\n\n"
            f"1. Model may need more GPU/CPU resources\n"
            f"2. First run - model might be loading\n"
            f"3. Close other applications to free resources"
        )

    elif "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
        return (
            f"WARNING: Model Not Found\n\n"
            f"The model '{model_name}' is not loaded in llama.cpp.\n\n"
            f"Ensure the model is loaded with llama-server."
        )
    else:
        return (
            f"WARNING: llama.cpp Error\n\n"
            f"An unexpected error occurred: {str(error)}\n\n"
            f"1. Check if llama-server is running\n"
            f"2. Check logs for details"
        )
