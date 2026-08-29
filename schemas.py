from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class UserQuery(BaseModel):
    city: str
    district: str
    message: str


class WeatherSuggestionRequest(BaseModel):
    user_id: Optional[str] = None
    city: str = "臺南市"
    district: str = "東區"
    message: str = ""
    activity: Optional[str] = None
    weather_data: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    user_id: Optional[str] = None
    message: str
    current_location: Optional[str] = None


class ChatCommandResponse(BaseModel):
    status: str = "success"
    reply: str = ""
    has_alert: bool = False
    alert_title: str = ""
    alert_url: str = ""
    action_type: Literal[
        "NONE",
        "ADD_EVENT",
        "CREATE_EVENT",
        "UPDATE_EVENT",
        "DELETE_EVENT",
        "CLARIFY",
        "EVENT_SYNCED",
        "WEATHER_QUERY",
        "DISASTER_GUIDE",
        "GAME_START",
    ] = "NONE"
    missing_slots: List[str] = Field(default_factory=list)
    clarify_slot: str = ""
    event_created: Dict[str, Any] = Field(default_factory=dict)
    event_updated: Dict[str, Any] = Field(default_factory=dict)
    weather_summary: Dict[str, Any] = Field(default_factory=dict)
    guideline: Dict[str, Any] = Field(default_factory=dict)
    game: Dict[str, Any] = Field(default_factory=dict)
    assistant_alerts: List[Dict[str, Any]] = Field(default_factory=list)
    event_title: str = ""
    event_start: str = ""
    event_end: str = ""
    event_id: str = ""
    event_city: str = ""
    event_district: str = ""
    event_location: str = ""
    event_id_to_delete: str = ""


class EventCreate(BaseModel):
    user_id: Optional[str] = None
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


class LocalAIRequest(BaseModel):
    title: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    activity: Optional[str] = None
    transport_type: Optional[str] = None
    weather: Dict[str, Any] = Field(default_factory=dict)
    risk_level: Optional[str] = None
    risk_tags: List[str] = Field(default_factory=list)


class AIIntentSuggestion(BaseModel):
    intent: str = "commuting"
    risk_summary: str = ""
    recommended_action: str = ""
    alternative_location: str = ""
    confidence: float = 0.0
    suggestion_source: str = "local_rules"
    matched_rules: List[str] = Field(default_factory=list)
    cache_hit: bool = False


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


class QuizScoreSubmitRequest(BaseModel):
    user_id: Optional[str] = None
    topic: Optional[str] = None
    score: int
    is_verified: bool = False


class GeocodeRequest(BaseModel):
    query: str


class WatchAreaCreate(BaseModel):
    user_id: str
    label: Optional[str] = None
    city: str
    district: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    is_active: bool = True


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


