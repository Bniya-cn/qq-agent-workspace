"""Image Agent V2 核心数据契约（TypedDict，零外部框架依赖）。"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

ReferenceSource = Literal[
    "upload",
    "reply",
    "session_generated",
    "session_source",
    "local_ref",
]

RoleConfidence = Literal["user_specified", "inferred", "unknown"]

TaskType = Literal[
    "t2i",
    "i2i_edit",
    "character_edit",
    "style_transfer",
    "background_edit",
    "multi_ref_compose",
    "product",
    "poster",
    "inpaint_local",
]

PromptMode = Literal["auto", "strict", "raw"]

ErrorClass = Literal[
    "",
    "auth",
    "param",
    "safety",
    "network",
    "model",
    "download",
    "incompatible",
]


class ReferenceImage(TypedDict, total=False):
    index: int
    source: ReferenceSource
    data_uri: str
    local_path: str
    qq_file_id: str
    roles: list[str]
    role_confidence: RoleConfidence


class ImageTaskPlan(TypedDict, total=False):
    task_type: TaskType
    user_intent: str
    prompt_mode: PromptMode
    reference_images: list[ReferenceImage]
    subject_identity: str
    preserve: list[str]
    modify: list[str]
    composition: str
    style: str
    negative: list[str]
    size: str
    model_hint: str
    needs_identity: bool
    prompt_extend: bool
    raw_user_prompt: str


class ImageGenerationRequest(TypedDict, total=False):
    model: str
    plan: ImageTaskPlan
    compiled_prompt: str
    images: list[str]
    parameters: dict[str, Any]
    truncated_notice: str


class ImageGenerationResult(TypedDict, total=False):
    ok: bool
    error_class: ErrorClass
    error_message: str
    model_used: str
    image_url: str
    local_path: str
    billed: bool
    request_id: str
    truncated_notice: str


class ImageSessionData(TypedDict, total=False):
    session_id: str
    user_id: str
    group_id: str
    source_images: list[str]
    generated_images: list[str]
    current_image: str
    parent_image: str
    original_prompt: str
    compiled_prompt: str
    task_plan: dict[str, Any]
    model_used: str
    created_at: float
    updated_at: float
