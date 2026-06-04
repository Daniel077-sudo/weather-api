from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

class UserQuery(BaseModel):
    city: str
    district: str
    message: str


class EventCreate(BaseModel):
    title: str
    start_time: str
    end_time: str
    city: Optional[str] = None
    district: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None
    transport_type: Optional[str] = None
    has_weather_risk: bool = False
    ai_suggestion: Optional[str] = None
    risk_level: Optional[str] = None
    risk_tags: List[str] = Field(default_factory=list)
    recommended_action: Optional[str] = None
    weather_snapshot: Optional[Dict[str, Any]] = None
    external_source: Optional[str] = None
    external_event_id: Optional[str] = None
    last_synced_at: Optional[str] = None


class EventRiskCheckRequest(BaseModel):
    title: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    activity: Optional[str] = "commuting"
    transport_type: Optional[str] = None
    event_id: Optional[str] = None


class AIIntentSuggestion(BaseModel):
    intent: str = "commuting"
    risk_summary: str = ""
    recommended_action: str = ""
    alternative_location: str = ""
    confidence: float = 0.0


class GameSubmitRequest(BaseModel):
    question_id: str
    selected_index: int
    game_type: Optional[str] = None


class GameScoreCreate(BaseModel):
    player_name: Optional[str] = "guest"
    game_type: str
    score: int
    total_questions: Optional[int] = None
    correct_count: Optional[int] = None


class GeocodeRequest(BaseModel):
    query: str


class EmergencyKitVisionResult(BaseModel):
    user_id: Optional[str] = None
    kit_id: Optional[str] = None
    detected_items: List[str] = Field(default_factory=list)
    missing_items: List[str] = Field(default_factory=list)
    confidence: float = 0.0


class EmergencyKitVisionRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    user_id: Optional[str] = None
    kit_id: Optional[str] = None


