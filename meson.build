project('site-safety-vision', 'cpp',
  version : '0.1.0',
  default_options : ['cpp_std=c++20', 'cuda_std=c++20', 'warning_level=2'],
  meson_version : '>= 1.1',
)

gst_deps = dependency('gstreamer-1.0')
gst_base_dep = dependency('gstreamer-base-1.0')
gst_app_dep = dependency('gstreamer-app-1.0')
gst_video_dep = dependency('gstreamer-video-1.0')
gst_gl_dep = dependency('gstreamer-gl-1.0')
gst_check_dep = dependency('gstreamer-check-1.0')
gtk_dep = dependency('gtk+-3.0')
yaml_dep = dependency('yaml-cpp')
hiredis_dep = dependency('hiredis', required : false)
json_dep = dependency('nlohmann_json')

tensorrt_mode = get_option('tensorrt_mode')
tensorrt_dep = declare_dependency()
tensorrt_enabled = false
if tensorrt_mode != 'disabled'
  tensorrt_dep = dependency('nvinfer', required : tensorrt_mode == 'enabled')
  tensorrt_enabled = tensorrt_dep.found()
endif
if tensorrt_enabled
  message('TensorRT backend enabled')
else
  message('TensorRT backend disabled; using explicit unavailable backend')
endif

cuda_enabled = false
if tensorrt_enabled
  cuda_enabled = add_languages('cuda', native : false, required : false)
  if cuda_enabled
    message('CUDA preprocessing kernel enabled')
  else
    message('CUDA compiler not found; using preprocessing fallback stub')
  endif
endif

onnx_dep = dependency('onnxruntime', required : true)
onnxruntime_profile = get_option('onnxruntime_profile')
onnxruntime_dependency_signature = get_option('onnxruntime_dependency_signature')

opencv_mode = get_option('opencv_mode')
opencv_enabled = opencv_mode == 'enabled'
opencv_dep = declare_dependency()
if opencv_enabled
  opencv_dep = dependency('opencv4', required : true)
  message('OpenCV enabled')
else
  message('OpenCV disabled')
endif

deps_runtime_path = get_option('deps_runtime_path')

conf_data = configuration_data()
conf_data.set_quoted('SSV_VERSION', meson.project_version())
conf_data.set_quoted('SSV_PACKAGE_NAME', 'site-safety-vision')

subdir('gst')
subdir('runner')
