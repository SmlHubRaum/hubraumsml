#!/bin/bash
ffmpeg -re -i /home/ubuntu/sml/bmx_cams_4_lower/bmx_lower/camera1_with_audio_360.m4v -vcodec copy -an -f rtp rtp://127.0.0.1:5040
