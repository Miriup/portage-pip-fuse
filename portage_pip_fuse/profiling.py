"""
Simple profiling utilities for tracking slow operations.
"""

import time
import functools
import logging
from collections import defaultdict
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Global timing stats
timing_stats: Dict[str, List[float]] = defaultdict(list)

def timed_operation(name: str = None):
    """
    Decorator to time function execution.
    
    Usage:
        @timed_operation("fetch_metadata")
        def my_function():
            ...
    """
    def decorator(func):
        operation_name = name or f"{func.__module__}.{func.__name__}"
        
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                elapsed = time.time() - start_time
                timing_stats[operation_name].append(elapsed)
                
                # Log if it took more than 0.1 seconds
                if elapsed > 0.1:
                    logger.info(f"SLOW: {operation_name} took {elapsed:.3f}s")
                elif elapsed > 0.01:
                    logger.debug(f"TIMING: {operation_name} took {elapsed:.3f}s")
        
        return wrapper
    return decorator

def print_timing_stats():
    """Print accumulated timing statistics."""
    if not timing_stats:
        print("No timing data collected")
        return
    
    print("\n" + "="*60)
    print("PERFORMANCE PROFILE SUMMARY")
    print("="*60)
    
    # Calculate totals and averages
    summary = []
    for operation, times in timing_stats.items():
        if times:
            total = sum(times)
            count = len(times)
            avg = total / count
            max_time = max(times)
            summary.append((total, operation, count, avg, max_time))
    
    # Sort by total time
    summary.sort(reverse=True)
    
    print(f"{'Operation':<40} {'Total':>10} {'Count':>8} {'Avg':>10} {'Max':>10}")
    print("-"*80)
    
    for total, operation, count, avg, max_time in summary[:20]:  # Top 20
        print(f"{operation:<40} {total:>10.3f} {count:>8} {avg:>10.3f} {max_time:>10.3f}")
    
    # Overall stats
    total_time = sum(s[0] for s in summary)
    total_calls = sum(s[2] for s in summary)
    print("-"*80)
    print(f"{'TOTAL':<40} {total_time:>10.3f} {total_calls:>8}")
    
def reset_timing_stats():
    """Reset timing statistics."""
    global timing_stats
    timing_stats.clear()

class TimingContext:
    """Context manager for timing code blocks."""
    
    def __init__(self, name: str):
        self.name = name
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.start_time
        timing_stats[self.name].append(elapsed)
        
        if elapsed > 0.1:
            logger.info(f"SLOW BLOCK: {self.name} took {elapsed:.3f}s")
        elif elapsed > 0.01:
            logger.debug(f"TIMING BLOCK: {self.name} took {elapsed:.3f}s")