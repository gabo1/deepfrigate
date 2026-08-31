CXX ?= g++
DS_ROOT ?= /opt/nvidia/deepstream/deepstream
CXXFLAGS := -std=c++17 -O2 -fPIC -Wall -Wextra
INCLUDES := -I$(DS_ROOT)/sources/includes -I/usr/local/cuda/include
TARGET := libnvdsinfer_custom_yolo26.so

$(TARGET): nvdsinfer_yolo26.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) -shared $< -o $@
