"""
Universal Pydantic models for shipment tracking data.

These models provide a common interface for tracking data from different providers.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict


class ShipmentStatus(str, Enum):
    """Standard shipment status values."""
    UNKNOWN = "unknown"
    INFORMATION_RECEIVED = "information_received"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    EXCEPTION = "exception"
    RETURNED = "returned"
    CANCELLED = "cancelled"


class TrackingEvent(BaseModel):
    """A single tracking event in a shipment's journey."""
    
    timestamp: datetime = Field(description="When the event occurred")
    status: str = Field(description="Human-readable status description")
    location: Optional[str] = Field(None, description="Location where event occurred")
    details: Optional[str] = Field(None, description="Additional details about the event")
    status_code: Optional[str] = Field(None, description="Provider-specific status code")
    
    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat()}
    )


class Shipment(BaseModel):
    """A single shipment with tracking information."""
    
    tracking_number: str = Field(description="The tracking/shipment number")
    carrier: str = Field(description="Name of the carrier/provider")
    status: ShipmentStatus = Field(description="Current shipment status")
    events: List[TrackingEvent] = Field(default_factory=list, description="Chronological list of tracking events")
    
    # Optional fields that may not be available from all providers
    service_type: Optional[str] = Field(None, description="Service type (express, standard, etc.)")
    origin: Optional[str] = Field(None, description="Origin location")
    destination: Optional[str] = Field(None, description="Destination location")
    estimated_delivery: Optional[datetime] = Field(None, description="Estimated delivery date")
    actual_delivery: Optional[datetime] = Field(None, description="Actual delivery date")
    
    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat()}
    )


class TrackingResponse(BaseModel):
    """Response containing one or more shipments."""
    
    shipments: List[Shipment] = Field(description="List of tracked shipments")
    provider: str = Field(description="Name of the tracking provider")
    query_timestamp: datetime = Field(default_factory=datetime.now, description="When the tracking was performed")
    
    @property
    def has_shipments(self) -> bool:
        """Check if response contains any shipments."""
        return len(self.shipments) > 0
    
    @property
    def primary_shipment(self) -> Optional[Shipment]:
        """Get the first (primary) shipment if available."""
        return self.shipments[0] if self.shipments else None
    
    model_config = ConfigDict(
        json_encoders={datetime: lambda v: v.isoformat()}
    )
