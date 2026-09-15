ssv_common_inc = include_directories('include')

ssv_common_lib = shared_library('ssv_common',
  'frame/ssv_analysis_frame.cpp',
  'config/ssv_config.cpp',
  'meta/ssv_meta.cpp',
  'frame/ssv_preprocess_transform.cpp',
  include_directories : ssv_common_inc,
  dependencies : [gst_deps, gst_video_dep, yaml_dep, json_dep],
  install : true,
)

ssv_common_dep = declare_dependency(
  link_with : ssv_common_lib,
  include_directories : ssv_common_inc,
  dependencies : [gst_deps, gst_video_dep, yaml_dep, json_dep],
)

subdir('tests')
