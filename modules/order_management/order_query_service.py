# modules/order_management/order_query_service.py
"""
Order Query Service Implementation

This service provides thread-safe, read-only access to order data.
It implements CQRS by separating queries from commands.
"""

import logging
import time
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

from interfaces.order_management.services import IOrderQueryService
from interfaces.order_management.state_service import IOrderStateService
from data_models.order_data_types import Order
from data_models import OrderStatus, OrderSide

logger = logging.getLogger(__name__)


class OrderQueryService(IOrderQueryService):
    """
    Concrete implementation of IOrderQueryService.
    
    Provides thread-safe, read-only access to order data by querying
    the IOrderStateService. Handles all filtering, formatting, and
    aggregation logic.
    """
    
    def __init__(self, state_service: IOrderStateService):
        """
        Initialize the query service with a state service.
        
        Args:
            state_service: The order state service to query
        """
        self.logger = logger
        self._state_service = state_service
        logger.info("OrderQueryService initialized")

    # ===== Basic Query Methods =====
    
    def get_order_by_local_id(self, local_id: int) -> Optional[Order]:
        """
        Retrieves a single order by its local ID.
        
        Delegates directly to the state service.
        """
        return self._state_service.get_order(local_id)

    def get_all_orders(self) -> List[Order]:
        """
        Retrieves all orders.
        
        Delegates directly to the state service.
        """
        return self._state_service.get_all_orders_snapshot()

    # ===== Filtered Query Methods =====
    
    def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """
        Retrieves all non-terminal orders, optionally filtered by symbol.
        
        Args:
            symbol: Optional symbol to filter by
            
        Returns:
            List of open orders
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        # Define terminal statuses
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED
        }
        
        open_orders = []
        for order in all_orders:
            # Check if order is not in a terminal state
            if order.status not in terminal_statuses:
                # Apply symbol filter if provided
                if symbol is None or order.symbol.upper() == symbol.upper():
                    open_orders.append(order)
        
        return open_orders

    def get_orders_for_trade(self, trade_id: int) -> List[Dict[str, Any]]:
        """
        Gets all orders associated with a specific trade.
        
        Args:
            trade_id: The parent trade ID
            
        Returns:
            List of order dictionaries for the trade
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        trade_orders = []
        for order in all_orders:
            if order.parent_trade_id == trade_id:
                # Convert to dictionary format for backward compatibility
                order_dict = {
                    'local_id': order.local_id,
                    'symbol': order.symbol,
                    'side': order.side,
                    'requested_shares': order.requested_shares,
                    'filled_quantity': order.filled_quantity,
                    'status': order.status.value,
                    'event_type': order.event_type,
                    'requested_lmt_price': order.requested_lmt_price,
                    'ls_order_id': order.ls_order_id,
                    'broker_order_id': order.broker_order_id,
                    'fills': [fill.__dict__ for fill in order.fills] if order.fills else []
                }
                trade_orders.append(order_dict)
        
        return trade_orders

    # ===== Calculation Methods =====
    
    def get_leftover_shares(self, local_id: int) -> float:
        """
        Gets the number of unfilled shares for an order.
        
        Args:
            local_id: The order ID
            
        Returns:
            Number of unfilled shares, or 0 if order not found
        """
        order = self._state_service.get_order(local_id)
        if not order:
            return 0.0
        
        return max(0.0, order.requested_shares - order.filled_quantity)

    def is_order_final(self, local_id: int) -> bool:
        """
        Checks if an order is in a terminal state.
        
        Args:
            local_id: The order ID
            
        Returns:
            True if order is final or doesn't exist
        """
        order = self._state_service.get_order(local_id)
        if not order:
            # Non-existent order is considered "final"
            return True
        
        terminal_statuses = {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED
        }
        
        return order.status in terminal_statuses

    def compute_leftover_shares_for_trade(self, trade_id: int) -> float:
        """
        Computes the total unfilled shares for all orders in a trade.
        
        Args:
            trade_id: The parent trade ID
            
        Returns:
            Total unfilled shares across all orders
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        total_leftover = 0.0
        for order in all_orders:
            if order.parent_trade_id == trade_id:
                leftover = max(0.0, order.requested_shares - order.filled_quantity)
                total_leftover += leftover
        
        return total_leftover

    def get_cumulative_filled_for_trade(self, parent_trade_id: int) -> float:
        """
        Computes the total filled shares for all orders in a trade.
        
        Args:
            parent_trade_id: The parent trade ID
            
        Returns:
            Total filled quantity across all orders
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        cumulative_filled = 0.0
        for order in all_orders:
            if order.parent_trade_id == parent_trade_id:
                cumulative_filled += order.filled_quantity
        
        return cumulative_filled

    # ===== Position Calculation Methods =====
    
    def get_symbol_total_fills(self, symbol: str) -> float:
        """
        Gets the net filled position for a symbol.
        
        Args:
            symbol: The stock symbol
            
        Returns:
            Net position (positive for long, negative for short)
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        symbol_upper = symbol.upper()
        
        total_fills = 0.0
        for order in all_orders:
            if order.symbol.upper() == symbol_upper:
                # Only count actual fills
                if order.filled_quantity > 0:
                    if order.side == OrderSide.BUY:
                        total_fills += order.filled_quantity
                    else:  # SELL
                        total_fills -= order.filled_quantity
        
        return total_fills

    def get_fill_calculated_position(self, symbol: str) -> float:
        """
        Calculates position purely from processed fills.
        
        This is the same as get_symbol_total_fills but kept
        for backward compatibility.
        
        Args:
            symbol: The stock symbol
            
        Returns:
            Net position (positive for long, negative for short)
        """
        return self.get_symbol_total_fills(symbol)

    # ===== Account Data Methods =====
    
    def get_account_data(self) -> Dict[str, Any]:
        """
        Gets aggregated account data.
        
        Returns:
            Dictionary with account statistics
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        # Calculate account statistics
        total_orders = len(all_orders)
        open_orders = len(self.get_open_orders())
        filled_orders = sum(1 for order in all_orders if order.status == OrderStatus.FILLED)
        cancelled_orders = sum(1 for order in all_orders if order.status == OrderStatus.CANCELLED)
        rejected_orders = sum(1 for order in all_orders if order.status == OrderStatus.REJECTED)
        
        # Calculate position summary
        positions = {}
        for order in all_orders:
            if order.filled_quantity > 0:
                symbol = order.symbol
                if symbol not in positions:
                    positions[symbol] = 0.0
                if order.side == OrderSide.BUY:
                    positions[symbol] += order.filled_quantity
                else:
                    positions[symbol] -= order.filled_quantity
        
        return {
            'total_orders': total_orders,
            'open_orders': open_orders,
            'filled_orders': filled_orders,
            'cancelled_orders': cancelled_orders,
            'rejected_orders': rejected_orders,
            'positions': positions,
            'timestamp': time.time()
        }

    def get_broker_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Gets the calculated position for a symbol.
        
        Args:
            symbol: The stock symbol
            
        Returns:
            Position data or None if no position
        """
        position = self.get_symbol_total_fills(symbol)
        
        if position == 0:
            return None
        
        return {
            'symbol': symbol.upper(),
            'quantity': position,
            'side': 'LONG' if position > 0 else 'SHORT',
            'absolute_quantity': abs(position)
        }

    def get_all_broker_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Gets all calculated positions.
        
        Returns:
            Dictionary mapping symbols to position data
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        # Calculate positions by symbol
        positions = {}
        for order in all_orders:
            if order.filled_quantity > 0:
                symbol = order.symbol.upper()
                if symbol not in positions:
                    positions[symbol] = 0.0
                if order.side == OrderSide.BUY:
                    positions[symbol] += order.filled_quantity
                else:
                    positions[symbol] -= order.filled_quantity
        
        # Format positions
        result = {}
        for symbol, quantity in positions.items():
            if quantity != 0:
                result[symbol] = {
                    'symbol': symbol,
                    'quantity': quantity,
                    'side': 'LONG' if quantity > 0 else 'SHORT',
                    'absolute_quantity': abs(quantity)
                }
        
        return result

    # ===== Export Methods =====
    
    def export_orders_to_csv(self, filepath: str) -> None:
        """
        Exports all orders to a CSV file.
        
        Args:
            filepath: Path to the output CSV file
        """
        import csv
        
        all_orders = self._state_service.get_all_orders_snapshot()
        
        with open(filepath, 'w', newline='') as csvfile:
            fieldnames = [
                'local_id', 'symbol', 'side', 'event_type', 'requested_shares',
                'filled_quantity', 'status', 'requested_lmt_price', 'avg_fill_price',
                'parent_trade_id', 'ls_order_id', 'broker_order_id', 'req_time'
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            for order in all_orders:
                # Calculate average fill price
                avg_fill_price = 0.0
                if order.filled_quantity > 0 and order.fills:
                    total_value = sum(fill.fill_quantity * fill.fill_price for fill in order.fills)
                    avg_fill_price = total_value / order.filled_quantity
                
                writer.writerow({
                    'local_id': order.local_id,
                    'symbol': order.symbol,
                    'side': order.side.value,
                    'event_type': order.event_type,
                    'requested_shares': order.requested_shares,
                    'filled_quantity': order.filled_quantity,
                    'status': order.status.value,
                    'requested_lmt_price': order.requested_lmt_price,
                    'avg_fill_price': avg_fill_price,
                    'parent_trade_id': order.parent_trade_id,
                    'ls_order_id': order.ls_order_id,
                    'broker_order_id': order.broker_order_id,
                    'req_time': order.req_time
                })
        
        logger.info(f"Exported {len(all_orders)} orders to {filepath}")

    def export_account_summary_to_csv(self, filepath: str) -> None:
        """
        Exports account summary data to a CSV file.
        
        Args:
            filepath: Path to the output CSV file
        """
        import csv
        
        account_data = self.get_account_data()
        positions = self.get_all_broker_positions()
        
        with open(filepath, 'w', newline='') as csvfile:
            # Write account summary
            csvfile.write("Account Summary\n")
            csvfile.write(f"Total Orders,{account_data['total_orders']}\n")
            csvfile.write(f"Open Orders,{account_data['open_orders']}\n")
            csvfile.write(f"Filled Orders,{account_data['filled_orders']}\n")
            csvfile.write(f"Cancelled Orders,{account_data['cancelled_orders']}\n")
            csvfile.write(f"Rejected Orders,{account_data['rejected_orders']}\n")
            csvfile.write("\n")
            
            # Write positions
            csvfile.write("Positions\n")
            csvfile.write("Symbol,Quantity,Side\n")
            for symbol, pos_data in positions.items():
                csvfile.write(f"{symbol},{pos_data['quantity']},{pos_data['side']}\n")
        
        logger.info(f"Exported account summary to {filepath}")

    def get_orders_for_gui_bootstrap(self, filter_date: str = "today") -> List[Order]:
        """
        Gets orders filtered for GUI bootstrap display.
        
        Args:
            filter_date: Date filter ("today", "yesterday", or "all")
            
        Returns:
            Filtered list of orders
        """
        all_orders = self._state_service.get_all_orders_snapshot()
        
        if filter_date == "all":
            return all_orders
        
        # Calculate date boundaries
        now = datetime.now()
        if filter_date == "today":
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_timestamp = start_of_day.timestamp()
        elif filter_date == "yesterday":
            yesterday = now - timedelta(days=1)
            start_of_day = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)
            start_timestamp = start_of_day.timestamp()
            end_timestamp = end_of_day.timestamp()
        else:
            # Default to today
            start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_timestamp = start_of_day.timestamp()
        
        # Filter orders by date
        filtered_orders = []
        for order in all_orders:
            if filter_date == "today" and order.req_time >= start_timestamp:
                filtered_orders.append(order)
            elif filter_date == "yesterday" and start_timestamp <= order.req_time < end_timestamp:
                filtered_orders.append(order)
        
        # Sort by request time, most recent first
        filtered_orders.sort(key=lambda x: x.req_time, reverse=True)
        
        return filtered_orders