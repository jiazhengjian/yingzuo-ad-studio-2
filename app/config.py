from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv
import imageio_ffmpeg

from app.planner_transport import parse_fallback_on, parse_stage_routes

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_root: Path = field(default_factory=lambda: ROOT / "data")
    deepseek_api_key: str = ""
    tudou_api_key: str = ""
    planner_model: str = "deepseek-flash"
    planner_provider: str = "deepseek"
    video_model: str = "wan3.0"
    image_model: str = "gpt-image-2-all"
    planning_generation_strategy: str = "continuity_v1"
    direction_selection_enabled: bool = True
    progressive_planning_enabled: bool = True
    joint_planning_enabled: bool = False
    content_draft_enabled: bool = False
    speech_draft_enabled: bool = False
    planner_storyboard_effort: str = "high"
    planner_direction_review_effort: str | None = None
    planner_stage_routes: dict = field(default_factory=dict)
    planner_fallback_provider: str = ""
    planner_fallback_model: str = ""
    planner_fallback_on: tuple = ()
    planner_stream: str = "off"
    planner_request_deadline_seconds: int | None = None
    planner_max_refinements: int = 2
    planner_refinement_format: str = "sparse_or_full"
    planner_speculate_recommended: bool = False
    state_references_enabled: bool = True
    image_concurrency: int = 3
    video_duration_seconds: int = 10
    max_video_submissions: int = 6
    max_active_runs: int = 1
    poll_seconds: float = 5
    # Unpaid HTTP refusals only. Empty disables the in-job re-POST.
    submission_retry_delays: tuple = (20, 60, 120)
    provider_timeout_seconds: int = 1800
    tts_voice: str = "Tingting"
    ffmpeg_path: str = field(default_factory=imageio_ffmpeg.get_ffmpeg_exe)

    @property
    def media_root(self):
        return self.data_root / "media"

    @property
    def database_path(self):
        return self.data_root / "studio.sqlite3"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=True)
        provider = os.getenv("PLANNER_PROVIDER", "deepseek").strip().lower()
        if provider not in ("deepseek", "tudou"):
            raise ValueError("PLANNER_PROVIDER must be deepseek or tudou")
        default_model = "gpt-6-sol" if provider == "tudou" else os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        joint = os.getenv("JOINT_PLANNING_ENABLED", "false").strip().lower()
        content_draft = os.getenv("CONTENT_DRAFT_ENABLED", "false").strip().lower()
        speech_draft = os.getenv("SPEECH_DRAFT_ENABLED", "false").strip().lower()
        effort = os.getenv("PLANNER_STORYBOARD_EFFORT", "high").strip().lower()
        if joint not in ("true", "false"):
            raise ValueError("JOINT_PLANNING_ENABLED must be true or false")
        if content_draft not in ("true", "false"):
            raise ValueError("CONTENT_DRAFT_ENABLED must be true or false")
        if speech_draft not in ("true", "false"):
            raise ValueError("SPEECH_DRAFT_ENABLED must be true or false")
        if effort not in ("low", "medium", "high"):
            raise ValueError("PLANNER_STORYBOARD_EFFORT must be low, medium or high")
        direction_effort = os.getenv("PLANNER_DIRECTION_REVIEW_EFFORT", "").strip().lower()
        if direction_effort and direction_effort not in ("low", "medium", "high"):
            raise ValueError("PLANNER_DIRECTION_REVIEW_EFFORT must be low, medium or high")
        stream = os.getenv("PLANNER_STREAM", "off").strip().lower()
        if stream not in ("off", "on", "auto"):
            raise ValueError("PLANNER_STREAM must be off, on or auto")
        refinement_format = os.getenv("PLANNER_REFINEMENT_FORMAT", "sparse_or_full").strip().lower()
        if refinement_format not in ("sparse_or_full", "sparse"):
            raise ValueError("PLANNER_REFINEMENT_FORMAT must be sparse_or_full or sparse")
        speculate = os.getenv("PLANNER_SPECULATE_RECOMMENDED", "false").strip().lower()
        if speculate not in ("true", "false"):
            raise ValueError("PLANNER_SPECULATE_RECOMMENDED must be true or false")
        state_refs = os.getenv("AD_STATE_REFERENCES", "").strip().lower()
        if state_refs in ("", "1", "true", "on", "yes"):
            state_references_enabled = True
        elif state_refs in ("0", "false", "off", "no"):
            state_references_enabled = False
        else:
            raise ValueError("AD_STATE_REFERENCES must be on or off")
        concurrency = os.getenv("IMAGE_CONCURRENCY", "3").strip()
        if not concurrency.isdigit() or not 1 <= int(concurrency) <= 8:
            raise ValueError("IMAGE_CONCURRENCY must be an integer from 1 to 8")
        refinements = os.getenv("PLANNER_MAX_REFINEMENTS", "2").strip()
        if refinements not in ("0", "1", "2"):
            raise ValueError("PLANNER_MAX_REFINEMENTS must be 0, 1 or 2")
        deadline_text = os.getenv("PLANNER_REQUEST_DEADLINE_SECONDS", "").strip()
        if deadline_text and (not deadline_text.isdigit() or not 30 <= int(deadline_text) <= 600):
            raise ValueError("PLANNER_REQUEST_DEADLINE_SECONDS must be an integer from 30 to 600")
        fallback_on = parse_fallback_on(os.getenv("PLANNER_FALLBACK_ON", ""))
        fallback_provider = os.getenv("PLANNER_FALLBACK_PROVIDER", "").strip().lower()
        fallback_model = os.getenv("PLANNER_FALLBACK_MODEL", "").strip()
        if fallback_on and (fallback_provider not in ("deepseek", "tudou") or not fallback_model):
            raise ValueError("PLANNER_FALLBACK_ON requires PLANNER_FALLBACK_PROVIDER and PLANNER_FALLBACK_MODEL")
        return cls(
            data_root=Path(os.getenv("AD_DATA_ROOT", str(ROOT / "data"))).resolve(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            tudou_api_key=os.getenv("TUDOU_API_KEY", "").strip(),
            joint_planning_enabled=joint == "true",
            content_draft_enabled=content_draft == "true",
            speech_draft_enabled=speech_draft == "true",
            planner_storyboard_effort=effort,
            planner_direction_review_effort=direction_effort or None,
            planner_stage_routes=parse_stage_routes(os.getenv("PLANNER_STAGE_ROUTES", "")),
            planner_fallback_provider=fallback_provider,
            planner_fallback_model=fallback_model,
            planner_fallback_on=fallback_on,
            planner_stream=stream,
            planner_request_deadline_seconds=int(deadline_text) if deadline_text else None,
            planner_max_refinements=int(refinements),
            planner_refinement_format=refinement_format,
            planner_speculate_recommended=speculate == "true",
            state_references_enabled=state_references_enabled,
            image_concurrency=int(concurrency),
            planner_provider=provider,
            planner_model=os.getenv("PLANNER_MODEL", "").strip() or default_model,
            tts_voice=os.getenv("TTS_VOICE", "Tingting"),
        )
