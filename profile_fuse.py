#!/usr/bin/env python3
"""
Profile the FUSE filesystem to identify performance bottlenecks.
"""

import cProfile
import pstats
import io
import sys
import os
from pathlib import Path

# Add the package to path
sys.path.insert(0, str(Path(__file__).parent))

from portage_pip_fuse.cli import main

def profile_with_cprofile():
    """Profile using cProfile and print sorted stats."""
    pr = cProfile.Profile()
    pr.enable()
    
    try:
        # Run the main CLI
        main()
    except KeyboardInterrupt:
        pass
    finally:
        pr.disable()
        
        # Print stats sorted by cumulative time
        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats('cumulative')
        ps.print_stats(50)  # Top 50 functions
        print(s.getvalue())
        
        # Also save to file
        pr.dump_stats('fuse_profile.stats')
        print("\nProfile saved to fuse_profile.stats")
        print("View with: python3 -m pstats fuse_profile.stats")

if __name__ == "__main__":
    profile_with_cprofile()