import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from yukti.data.state import get_all_positions
from yukti.data.models import Position
from datetime import datetime

@pytest.mark.asyncio
async def test_get_all_positions_empty():
    """Test get_all_positions when no positions exist in DB."""
    mock_db = AsyncMock()
    mock_db.execute.return_value.scalars.return_value = []
    
    with patch("yukti.data.state.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db
        
        positions = await get_all_positions()
        assert positions == {}
        mock_db.execute.assert_called_once()

@pytest.mark.asyncio
async def test_get_all_positions_with_data():
    """Test get_all_positions returns mapped dictionary of positions."""
    mock_pos = MagicMock(spec=Position)
    mock_pos.id = "1"
    mock_pos.symbol = "RELIANCE"
    mock_pos.security_id = "1333"
    mock_pos.direction = "LONG"
    mock_pos.setup_type = "EMA"
    mock_pos.holding_period = "intraday"
    mock_pos.entry_price = 2500.0
    mock_pos.fill_price = 2505.0
    mock_pos.stop_loss = 2480.0
    mock_pos.target_1 = 2550.0
    mock_pos.target_2 = None
    mock_pos.quantity = 10
    mock_pos.conviction = 8
    mock_pos.risk_reward = 2.0
    mock_pos.intent_id = "int-1"
    mock_pos.entry_order_id = "ord-1"
    mock_pos.sl_gtt_id = "gtt-1"
    mock_pos.target_gtt_id = "gtt-2"
    mock_pos.status = "OPEN"
    mock_pos.reasoning = "Test reasoning"
    mock_pos.opened_at = datetime(2024, 1, 1, 9, 15)
    mock_pos.filled_at = datetime(2024, 1, 1, 9, 20)

    mock_db = AsyncMock()
    mock_db.execute.return_value.scalars.return_value = [mock_pos]
    
    with patch("yukti.data.state.get_db") as mock_get_db:
        mock_get_db.return_value.__aenter__.return_value = mock_db
        
        positions = await get_all_positions()
        assert "RELIANCE" in positions
        assert positions["RELIANCE"]["symbol"] == "RELIANCE"
        assert positions["RELIANCE"]["entry_price"] == 2500.0
        assert positions["RELIANCE"]["opened_at"] == "2024-01-01T09:15:00"
