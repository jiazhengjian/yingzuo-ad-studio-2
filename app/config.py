from dataclasses import dataclass, field
from pathlib import Path
import os
from dotenv import load_dotenv
import imageio_ffmpeg

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
    video_duration_seconds: int = 10
    max_video_submissions: int = 6
    max_active_runs: int = 1
    poll_seconds: float = 5
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
        return cls(
            data_root=Path(os.getenv("AD_DATA_ROOT", str(ROOT / "data"))).resolve(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
            tudou_api_key=os.getenv("TUDOU_API_KEY", "").strip(),
            joint_planning_enabled=joint == "true",
            content_draft_enabled=content_draft == "true",
            speech_draft_enabled=speech_draft == "true",
            planner_storyboard_effort=effort,
            planner_provider=provider,
            planner_model=os.getenv("PLANNER_MODEL", "").strip() or default_model,
            tts_voice=os.getenv("TTS_VOICE", "Tingting"),
        )
