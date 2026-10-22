"""Platform takeover policy, separate from legacy TTS compatibility."""

# Product announcements by Xiaomi Smart Ecosystem:
# https://www.weibo.com/5836533026/Pggd7w9L8
# https://www.weibo.com/5836533026/PpthfitTM
# Rollout announcement: https://weibo.com/u/5836533026?index=1289&tabtype=album&uid=5836533026
# Hardware mapping: https://github.com/idootop/mi-gpt/blob/main/docs/compatibility.md
# Add models only after verifying both native AI capability and hardware ID.
# A user-editable nickname is never evidence of native AI capability.
NATIVE_AI_MODELS = {
    'OH2': 'Xiaomi 智能音箱', 'OH2P': 'Xiaomi 智能音箱 Pro',
    'L17A': 'Xiaomi Sound Pro', 'X10A': 'Xiaomi 智能家庭屏 10',
}
NATIVE_AI_REASON = '该机型自带超级小爱 AI 大模型，无需平台接管，暂不支持启用'


def hardware_code(value):
    """Accept MiNA hardware codes and MIoT model identifiers."""
    return str(value or '').strip().upper().rsplit('.', 1)[-1]


def native_ai(value):
    return hardware_code(value) in NATIVE_AI_MODELS
