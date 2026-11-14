"""
Backward Compatibility Wrapper: bear_flag_engine -> bidirectional_engine.
All strategy tracking, strike resolution, risk management, and multi-simulation
paper portfolio execution logic now resides in bidirectional_engine.py.
"""
import sys
import os

from bidirectional_engine import *
