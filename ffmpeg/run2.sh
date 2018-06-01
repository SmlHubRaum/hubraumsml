#!/bin/bash
ffmpeg -re -i /home/ubuntu/sml/bmx_cams_4_lower/bmx_lower/camera1_with_audio_720.m4v -strict -2 -f rtp_mpegts rtp://127.0.0.1:5040 -strict -2


