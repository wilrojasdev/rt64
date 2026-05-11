cmake_minimum_required(VERSION 3.20)
project(rt64 LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)

if (APPLE)
    enable_language(OBJC OBJCXX)
    set(CMAKE_OSX_DEPLOYMENT_TARGET "10.15")
endif()

option(RT64_STATIC "Build RT64 as a static library" OFF)


option(RT64_SDL_WINDOW_VULKAN "Build RT64 to expect an SDL Window outside of Windows" OFF)

# Android spike mode: skip host-side tools (file_to_c, texture_hasher, ...) and
# skip shader compilation. Lets us measure C++ portability of rt64 in isolation.
option(RT64_ANDROID_SPIKE "Skip shaders and host tools for Android cross-compile spike" OFF)

if (NOT ${RT64_STATIC})
    set(CMAKE_POSITION_INDEPENDENT_CODE ON)
endif()

function(preprocess INFILE OUTFILE OPTIONS)
    if (CMAKE_CXX_COMPILER_FRONTEND_VARIANT STREQUAL "MSVC")
        if (CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
            add_custom_command(OUTPUT ${OUTFILE}
                COMMAND clang -x c -E -P ${INFILE} -o ${OUTFILE} ${OPTIONS}
                DEPENDS ${INFILE})
        else()
            add_custom_command(OUTPUT ${OUTFILE}
                COMMAND ${CMAKE_CXX_COMPILER} /Zs /EP ${INFILE} ${OPTIONS} > ${OUTFILE}
                DEPENDS ${INFILE})
        endif()
    else()
        add_custom_command(OUTPUT ${OUTFILE}
            COMMAND ${CMAKE_CXX_COMPILER} -x c -E -P ${INFILE} -o ${OUTFILE} ${OPTIONS}
            DEPENDS ${INFILE})
    endif()
endfunction()

# Target-side runtime concerns: NOMINMAX define and dxcompiler.dll next to the
# Windows binary.
if (WIN32)
    add_compile_definitions(NOMINMAX)

    # Dependencies that must be next to the DLL.
    configure_file("${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/x64/dxcompiler.dll" "dxcompiler.dll" COPYONLY)
    configure_file("${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/x64/dxil.dll" "dxil.dll" COPYONLY)
endif()

# DXC and spirv-cross run on the build host, not the target. Pick the binary
# matching the host platform so cross-compiles (e.g. macOS arm64 host targeting
# Android arm64) work correctly.
if (CMAKE_HOST_WIN32)
    set (DXC "${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/x64/dxc.exe")
elseif (CMAKE_HOST_APPLE)
    # find zlib and include in dyld path
    find_library(ZLIB_LIBRARY NAMES z)
    get_filename_component(ZLIB_PATH ${ZLIB_LIBRARY} DIRECTORY)

    if (CMAKE_HOST_SYSTEM_PROCESSOR STREQUAL "x86_64")
        set (DXC "DYLD_LIBRARY_PATH=${PROJECT_SOURCE_DIR}/src/contrib/dxc/lib/x64:${ZLIB_PATH}" "${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/x64/dxc-macos")
        set (SPIRVCROSS "DYLD_LIBRARY_PATH=${PROJECT_SOURCE_DIR}/src/contrib/spirv-cross/lib/x64:$ENV{DYLD_LIBRARY_PATH}" "${PROJECT_SOURCE_DIR}/src/contrib/spirv-cross/bin/x64/spirv-cross")
    else()
        set (DXC "DYLD_LIBRARY_PATH=${PROJECT_SOURCE_DIR}/src/contrib/dxc/lib/arm64:${ZLIB_PATH}" "${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/arm64/dxc-macos")
        set (SPIRVCROSS "DYLD_LIBRARY_PATH=${PROJECT_SOURCE_DIR}/src/contrib/spirv-cross/lib/arm64:$ENV{DYLD_LIBRARY_PATH}" "${PROJECT_SOURCE_DIR}/src/contrib/spirv-cross/bin/arm64/spirv-cross")
    endif()
else()
    if (CMAKE_HOST_SYSTEM_PROCESSOR STREQUAL "x86_64")
        set (DXC "LD_LIBRARY_PATH=${PROJECT_SOURCE_DIR}/src/contrib/dxc/lib/x64" "${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/x64/dxc-linux")
    else()
        set (DXC "LD_LIBRARY_PATH=${PROJECT_SOURCE_DIR}/src/contrib/dxc/lib/arm64" "${PROJECT_SOURCE_DIR}/src/contrib/dxc/bin/arm64/dxc-linux")
    endif()
endif()

set(ZSTD_LEGACY_SUPPORT OFF)
set(ZSTD_BUILD_STATIC ON)
set(ZSTD_BUILD_SHARED OFF)
if (ANDROID)
    # zstd's dictBuilder calls qsort_r(), which Bionic (Android libc) doesn't
    # provide. rt64 only uses compression/decompression, never dictionary
    # training, so dropping dictBuilder is safe and avoids vendoring a shim.
    # Programs (zstd CLI) and tests are also unnecessary for rt64.
    set(ZSTD_BUILD_DICTBUILDER OFF)
    set(ZSTD_BUILD_PROGRAMS OFF)
    set(ZSTD_BUILD_TESTS OFF)
endif()

if (ANDROID)
    # Cross-compile to Android: file_to_c must run on the host during build to
    # convert shader blobs into embedded .c arrays. file_to_c.cpp is a single
    # standalone translation unit (only standard library, no project deps) so
    # we skip a nested CMake — that path leaks the NDK toolchain via env vars
    # and pairs the wrong libc++ with the host system headers. Direct compile
    # is bullet-proof.
    if (CMAKE_HOST_APPLE)
        # Pin to /usr/bin/clang++ (Apple's). The NDK ships its own clang++ on
        # PATH; if find_program picks that up, its libc++ will mismatch the
        # macOS SDK headers and break <charconv>/<filesystem>.
        find_program(_HOST_CXX_COMPILER clang++ PATHS /usr/bin NO_DEFAULT_PATH REQUIRED)
        execute_process(
            COMMAND xcrun --show-sdk-path
            OUTPUT_VARIABLE _HOST_SYSROOT
            OUTPUT_STRIP_TRAILING_WHITESPACE
        )
        set(_HOST_EXTRA_FLAGS -isysroot ${_HOST_SYSROOT})
    elseif (CMAKE_HOST_WIN32)
        find_program(_HOST_CXX_COMPILER NAMES cl clang++ g++ REQUIRED)
        set(_HOST_EXTRA_FLAGS "")
    else()
        # On Linux pin to /usr/bin too, for the same reason.
        find_program(_HOST_CXX_COMPILER NAMES g++ clang++ c++ PATHS /usr/bin NO_DEFAULT_PATH REQUIRED)
        set(_HOST_EXTRA_FLAGS "")
    endif()

    set(FILE_TO_C_HOST_BIN ${CMAKE_BINARY_DIR}/host_tools/file_to_c)
    if (NOT EXISTS ${FILE_TO_C_HOST_BIN})
        message(STATUS "Compiling host file_to_c -> ${FILE_TO_C_HOST_BIN}")
        execute_process(
            COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_BINARY_DIR}/host_tools
        )
        execute_process(
            COMMAND ${_HOST_CXX_COMPILER}
                -std=c++17 -O2
                ${_HOST_EXTRA_FLAGS}
                -o ${FILE_TO_C_HOST_BIN}
                ${CMAKE_CURRENT_SOURCE_DIR}/src/tools/file_to_c/file_to_c.cpp
            RESULT_VARIABLE _file_to_c_result
        )
        if (NOT _file_to_c_result EQUAL 0)
            message(FATAL_ERROR "Failed to compile host file_to_c (exit ${_file_to_c_result})")
        endif()
    endif()
    add_executable(file_to_c IMPORTED GLOBAL)
    set_target_properties(file_to_c PROPERTIES IMPORTED_LOCATION ${FILE_TO_C_HOST_BIN})
elseif (NOT RT64_ANDROID_SPIKE)
    add_subdirectory(src/tools/file_to_c)
endif()

if (NOT RT64_ANDROID_SPIKE OR ANDROID)
    # re-spirv MUST be cross-compiled for Android: rt64_raster_shader.cpp
    # uses respv::Optimizer::run to inline spec constants into the SPIR-V
    # at pipeline-creation time, and the output is what's handed to
    # vkCreateShaderModule. Stubbing it out leaves codeSize=0 shaders that
    # SIGSEGV inside the Mali driver. SPIRV-Headers is header-only and
    # cross-compiles cleanly.
    add_subdirectory(src/contrib/re-spirv)
endif()
if (NOT RT64_ANDROID_SPIKE)
    add_subdirectory(src/contrib/nativefiledialog-extended)
endif()

# zstd cross-compiles cleanly to arm64-v8a; always include it.
# (re-spirv and nfd are deferred — re-spirv hasn't been audited for Android yet,
# and nfd is replaced by the Storage Access Framework on Android in Phase 7.)
add_subdirectory(src/contrib/zstd/build/cmake)

if ((CMAKE_SYSTEM_NAME MATCHES "Linux" OR ANDROID) AND RT64_SDL_WINDOW_VULKAN)
    set(PLUME_SDL_VULKAN_ENABLED TRUE)
elseif (APPLE)
    set(PLUME_APPLE_RETINA_ENABLED OFF)
endif()

add_subdirectory(src/contrib/plume)

if (CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
    # Use /W4 for clang-cl, as -Wall maps to MSVC's /Wall which in turn maps to clang's -Weverything
    if (CMAKE_CXX_SIMULATE_ID STREQUAL "MSVC")
        add_compile_options(/W4)
    else()
        add_compile_options(-Wall -Wextra)
    endif()
    add_compile_options(-Wno-nullability-completeness -Wno-unused-parameter -Wno-unused-variable -Wno-unused-function -Wno-unused-but-set-variable -Wno-missing-field-initializers -Wno-switch -Wno-tautological-undefined-compare)
    # imgui currently triggers some compiler warnings on clang 19 for function casts which leads to a failed build, so disable that warning on clang 19+
    if (CMAKE_CXX_COMPILER_VERSION VERSION_GREATER_EQUAL 19.0)
        add_compile_options(-Wno-cast-function-type-mismatch)
    endif()
    if (CMAKE_CXX_COMPILER_VERSION VERSION_GREATER_EQUAL 20.0)
        add_compile_options(-Wno-deprecated-literal-operator -Wno-nontrivial-memcall)
    endif()
endif()

set(ANDROID_PLATFORM android-24)
set(ANDROID_ABI arm64-v8a)

# For DXC
set (DXC_COMMON_OPTS "-I${PROJECT_SOURCE_DIR}/src")
set (DXC_DXIL_OPTS "-Wno-ignored-attributes")
set (DXC_SPV_OPTS "-spirv" "-fspv-target-env=vulkan1.0" "-fvk-use-dx-layout")
set (DXC_LB_OPTS "${DXC_COMMON_OPTS}" "-D" "LIBRARY" "-T" "lib_6_3")
set (DXC_PS_OPTS "${DXC_COMMON_OPTS}" "-E" "PSMain" "-T ps_6_3")
set (DXC_VS_OPTS "${DXC_COMMON_OPTS}" "-E" "VSMain" "-T vs_6_3" "-fvk-invert-y")
set (DXC_CS_OPTS "${DXC_COMMON_OPTS}" "-E" "CSMain" "-T cs_6_3")
set (DXC_GS_OPTS "${DXC_COMMON_OPTS}" "-E" "GSMain" "-T gs_6_3")
set (DXC_RT_OPTS "${DXC_COMMON_OPTS}" "-D" "RT_SHADER" "-T" "lib_6_3" "-fspv-target-env=vulkan1.1spirv1.4" "-fspv-extension=SPV_KHR_ray_tracing" "-fspv-extension=SPV_EXT_descriptor_indexing")

# RasterPS bring-up: pass -D RT64_DIAG_RASTER_PS_MODE=N to DXC (see RasterPS.hlsl).
set(RT64_DIAG_RASTER_PS_MODE "0" CACHE STRING "RasterPS: 0=normal, 1=solid magenta, 2=visualize RenderParams low bytes (omL/omH/flags)")
set(RT64_RASTER_PS_EXTRA_DXC "")
if(NOT RT64_DIAG_RASTER_PS_MODE STREQUAL "0")
    list(APPEND RT64_RASTER_PS_EXTRA_DXC "-D" "RT64_DIAG_RASTER_PS_MODE=${RT64_DIAG_RASTER_PS_MODE}")
endif()

# VideoInterface PS bring-up modes (see VideoInterfacePS.hlsl).
set(RT64_DIAG_VI_MODE "0" CACHE STRING "VideoInterfacePS: 0=normal, 1=solid cyan, 2=raw gInput sample (no AA/gamma), 3=UV grid, 4=sample classifier (red=saturated, green=zero, blue=real)")
set(RT64_VI_EXTRA_DXC "")
if(NOT RT64_DIAG_VI_MODE STREQUAL "0")
    list(APPEND RT64_VI_EXTRA_DXC "-D" "RT64_DIAG_VI_MODE=${RT64_DIAG_VI_MODE}")
endif()

# Mali Valhall G57 (and likely other dualSrcBlend-less drivers) route the
# pixel shader's SV_TARGET1 output into the color attachment when the
# pipeline has dualSrcBlend disabled — a spec violation that turns the
# whole frame into whatever color we wrote to pixelAlpha. When this option
# is ON the DYNAMIC ubershader is compiled without the SV_TARGET1 / pixelAlpha
# output entirely, so the driver has nothing to misroute. SPEC_CONSTANT
# variants keep the dual-source declaration (their DXC dead-code-elimination
# breaks if it's removed — see RasterPS.hlsl top-of-file comment).
option(RT64_NO_DUAL_SOURCE_DYNAMIC_PS "Strip SV_TARGET1/pixelAlpha from RasterPSDynamic variants" OFF)
set(RT64_DYNAMIC_PS_EXTRA_DXC "")
if(RT64_NO_DUAL_SOURCE_DYNAMIC_PS)
    list(APPEND RT64_DYNAMIC_PS_EXTRA_DXC "-D" "NO_DUAL_SOURCE")
endif()

# Same NO_DUAL_SOURCE strip, but for the SPEC_CONSTANT pipeline variants.
# Off by default because a previous attempt left DXC emitting a 440-byte
# stub for the spec-constant translation unit. With the current clean
# #ifndef NO_DUAL_SOURCE guards around both the SV_TARGET1 output and
# its assignment, that may not reproduce — re-test before committing to
# either path. When this DOES work, Android can route draws through
# the spec-constant pipelines (10-50× faster than the dynamic ubershader)
# and the force-ubershader hack in framebuffer_renderer.cpp can come out.
option(RT64_NO_DUAL_SOURCE_SPEC_CONST_PS "Strip SV_TARGET1/pixelAlpha from RasterPSSpecConstant variants" OFF)
set(RT64_SPEC_CONST_PS_EXTRA_DXC "")
if(RT64_NO_DUAL_SOURCE_SPEC_CONST_PS)
    list(APPEND RT64_SPEC_CONST_PS_EXTRA_DXC "-D" "NO_DUAL_SOURCE")
endif()

function(build_shader_spirv_impl TARGETOBJ FILENAME TARGET_NAME OUTNAME)
    # Sentinel arg: when "--strip-image-format-unknown" appears in the args,
    # filter it out before invoking DXC and append a post-process step that
    # rewrites every OpTypeImage Format operand in the .spv to Unknown.
    # Required because DXC 1.7 silently ignores [[vk::image_format("unknown")]],
    # which breaks format-agnostic framebuffer-manipulation compute shaders on
    # strict-aliasing Vulkan drivers (Mali Valhall G57).
    set(_dxc_args ${ARGN})
    set(_strip_image_format FALSE)
    if (_dxc_args)
        list(FIND _dxc_args "--strip-image-format-unknown" _strip_idx)
        if (_strip_idx GREATER -1)
            list(REMOVE_AT _dxc_args ${_strip_idx})
            set(_strip_image_format TRUE)
        endif()
    endif()
    if (_strip_image_format)
        add_custom_command(OUTPUT ${OUTNAME}.spv
            COMMAND ${DXC} ${DXC_SPV_OPTS} ${_dxc_args} ${FILENAME} /Fo ${OUTNAME}.spv
            COMMAND ${CMAKE_COMMAND} -E env python3 ${PROJECT_SOURCE_DIR}/scripts/spv_strip_image_format.py ${OUTNAME}.spv
            DEPENDS ${FILENAME} ${PROJECT_SOURCE_DIR}/scripts/spv_strip_image_format.py)
    else()
        add_custom_command(OUTPUT ${OUTNAME}.spv
            COMMAND ${DXC} ${DXC_SPV_OPTS} ${_dxc_args} ${FILENAME} /Fo ${OUTNAME}.spv
            DEPENDS ${FILENAME})
    endif()
    add_custom_command(OUTPUT ${OUTNAME}.spirv.c
        COMMAND file_to_c ${OUTNAME}.spv ${TARGET_NAME}BlobSPIRV ${OUTNAME}.spirv.c ${OUTNAME}.spirv.h
        DEPENDS ${OUTNAME}.spv file_to_c
        BYPRODUCTS ${OUTNAME}.spirv.h)
    target_sources(${TARGETOBJ} PRIVATE ${OUTNAME}.spirv.c)
endfunction()

function(build_shader_dxil_impl TARGETOBJ FILENAME TARGET_NAME OUTNAME)
    add_custom_command(OUTPUT ${OUTNAME}.dxil
        COMMAND ${DXC} ${DXC_DXIL_OPTS} ${ARGN} ${FILENAME} /Fo ${OUTNAME}.dxil
        DEPENDS ${FILENAME})
    add_custom_command(OUTPUT ${OUTNAME}.dxil.c
        COMMAND file_to_c ${OUTNAME}.dxil ${TARGET_NAME}BlobDXIL ${OUTNAME}.dxil.c ${OUTNAME}.dxil.h
        DEPENDS ${OUTNAME}.dxil file_to_c
        BYPRODUCTS ${OUTNAME}.dxil.h)
    target_sources(${TARGETOBJ} PRIVATE ${OUTNAME}.dxil.c)
endfunction()

function(build_shader_msl_impl TARGETOBJ FILENAME TARGET_NAME OUTNAME)
    add_custom_command(OUTPUT ${OUTNAME}.spv
            COMMAND ${DXC} ${DXC_SPV_OPTS} ${ARGN} ${FILENAME} /Fo ${OUTNAME}.spv
            DEPENDS ${FILENAME})
    add_custom_command(OUTPUT ${OUTNAME}.metal
            COMMAND spirv_cross_msl ${OUTNAME}.spv ${OUTNAME}.metal
            DEPENDS ${OUTNAME}.spv spirv_cross_msl)
    add_custom_command(OUTPUT ${OUTNAME}.ir
            COMMAND xcrun -sdk macosx metal -o ${OUTNAME}.ir -c ${OUTNAME}.metal $<$<CONFIG:Debug>:-frecord-sources>
            DEPENDS ${OUTNAME}.metal)
    add_custom_command(OUTPUT ${OUTNAME}.metallib
            COMMAND xcrun -sdk macosx metallib ${OUTNAME}.ir -o ${OUTNAME}.metallib
            DEPENDS ${OUTNAME}.ir)
    add_custom_command(OUTPUT ${OUTNAME}.metal.c
            COMMAND file_to_c ${OUTNAME}.metallib ${TARGET_NAME}BlobMSL ${OUTNAME}.metal.c ${OUTNAME}.metal.h
            DEPENDS ${OUTNAME}.metallib file_to_c
            BYPRODUCTS ${OUTNAME}.metal.h)
    target_sources(${TARGETOBJ} PRIVATE ${OUTNAME}.metal.c)
endfunction()

function(build_shader TARGETOBJ SHADERNAME OPTIONS)
    set(FILENAME "${PROJECT_SOURCE_DIR}/${SHADERNAME}")
    if (${ARGC} GREATER 3)
        set(OUTNAME "${CMAKE_BINARY_DIR}/${ARGV3}")
    else()
        set(OUTNAME "${CMAKE_BINARY_DIR}/${SHADERNAME}")
    endif()
    # Get any optional compiler args passed to this function
    if (${ARGC} GREATER 4)
        set(EXTRA_ARGS "${ARGN}")
        list(REMOVE_AT EXTRA_ARGS 0)
    endif()
    cmake_path(GET OUTNAME STEM TARGET_NAME)
    cmake_path(GET OUTNAME PARENT_PATH OUTPUT_DIR)
    file(MAKE_DIRECTORY ${OUTPUT_DIR})
    # Compile DXIL shader binaries if building on Windows
    if (WIN32)
        build_shader_dxil_impl(${TARGETOBJ} ${FILENAME} ${TARGET_NAME} ${OUTNAME} ${OPTIONS} ${EXTRA_ARGS})
    endif()
    # Compile Metal shader binaries if building on macOS
    if (APPLE)
        build_shader_msl_impl(${TARGETOBJ} ${FILENAME} ${TARGET_NAME} ${OUTNAME} ${OPTIONS} ${EXTRA_ARGS})
    endif()
    build_shader_spirv_impl(${TARGETOBJ} ${FILENAME} ${TARGET_NAME} ${OUTNAME} ${OPTIONS} ${EXTRA_ARGS})
endfunction()

function(build_shader_dxil TARGETOBJ SHADERNAME OPTIONS)
    set(FILENAME "${PROJECT_SOURCE_DIR}/${SHADERNAME}")
    if (${ARGC} GREATER 3)
        set(OUTNAME "${CMAKE_BINARY_DIR}/${ARGV3}")
    else()
        set(OUTNAME "${CMAKE_BINARY_DIR}/${SHADERNAME}")
    endif()
    # Get any optional compiler args passed to this function
    if (${ARGC} GREATER 4)
        set(EXTRA_ARGS "${ARGN}")
        list(REMOVE_AT EXTRA_ARGS 0)
    endif()
    cmake_path(GET OUTNAME STEM TARGET_NAME)
    cmake_path(GET OUTNAME PARENT_PATH OUTPUT_DIR)
    file(MAKE_DIRECTORY ${OUTPUT_DIR})
    # Compile DXIL shader binaries if building on Windows
    build_shader_dxil_impl(${TARGETOBJ} ${FILENAME} ${TARGET_NAME} ${OUTNAME} ${OPTIONS} ${EXTRA_ARGS})
endfunction()

function(build_shader_spirv TARGETOBJ SHADERNAME OPTIONS)
    set(FILENAME "${PROJECT_SOURCE_DIR}/${SHADERNAME}")
    if (${ARGC} GREATER 3)
        set(OUTNAME "${CMAKE_BINARY_DIR}/${ARGV3}")
    else()
        set(OUTNAME "${CMAKE_BINARY_DIR}/${SHADERNAME}")
    endif()
    # Get any optional compiler args passed to this function
    if (${ARGC} GREATER 4)
        set(EXTRA_ARGS "${ARGN}")
        list(REMOVE_AT EXTRA_ARGS 0)
    endif()
    cmake_path(GET OUTNAME STEM TARGET_NAME)
    cmake_path(GET OUTNAME PARENT_PATH OUTPUT_DIR)
    file(MAKE_DIRECTORY ${OUTPUT_DIR})
    build_shader_spirv_impl(${TARGETOBJ} ${FILENAME} ${TARGET_NAME} ${OUTNAME} ${OPTIONS} ${EXTRA_ARGS})
endfunction()

function(build_shader_msl TARGETOBJ SHADERNAME OPTIONS)
    set(FILENAME "${PROJECT_SOURCE_DIR}/${SHADERNAME}")
    if (${ARGC} GREATER 3)
        set(OUTNAME "${CMAKE_BINARY_DIR}/${ARGV3}")
    else()
        set(OUTNAME "${CMAKE_BINARY_DIR}/${SHADERNAME}")
    endif()
    # Get any optional compiler args passed to this function
    if (${ARGC} GREATER 4)
        set(EXTRA_ARGS "${ARGN}")
        list(REMOVE_AT EXTRA_ARGS 0)
    endif()
    cmake_path(GET OUTNAME STEM TARGET_NAME)
    cmake_path(GET OUTNAME PARENT_PATH OUTPUT_DIR)
    file(MAKE_DIRECTORY ${OUTPUT_DIR})
    build_shader_msl_impl(${TARGETOBJ} ${FILENAME} ${TARGET_NAME} ${OUTNAME} ${OPTIONS} ${EXTRA_ARGS})
endfunction()

function(preprocess_shader TARGETOBJ SHADERNAME)
    set(FILENAME "${PROJECT_SOURCE_DIR}/${SHADERNAME}")
    set(OUTNAME "${CMAKE_BINARY_DIR}/${SHADERNAME}")
    cmake_path(GET SHADERNAME STEM TARGET_NAME)
    cmake_path(GET OUTNAME PARENT_PATH OUTPUT_DIR)
    preprocess(${FILENAME} ${OUTNAME}.rw ${DXC_COMMON_OPTS})
    add_custom_command(OUTPUT ${OUTNAME}.rw.c
        COMMAND file_to_c ${OUTNAME}.rw ${TARGET_NAME}Text ${OUTNAME}.rw.c ${OUTNAME}.rw.h
        DEPENDS ${OUTNAME}.rw file_to_c
        BYPRODUCTS ${OUTNAME}.rw.h)
    file(MAKE_DIRECTORY ${OUTPUT_DIR})
    target_sources(${TARGETOBJ} PRIVATE ${OUTNAME}.rw.c)
endfunction()

function(build_library_shader TARGETOBJ SHADERNAME)
    build_shader_dxil(${TARGETOBJ} ${SHADERNAME} "${DXC_LB_OPTS}" ${ARGN})
endfunction()

function(build_pixel_shader TARGETOBJ SHADERNAME)
    build_shader(${TARGETOBJ} ${SHADERNAME} "${DXC_PS_OPTS}" ${ARGN})
endfunction()

function(build_vertex_shader TARGETOBJ SHADERNAME)
    build_shader(${TARGETOBJ} ${SHADERNAME} "${DXC_VS_OPTS}" ${ARGN})
endfunction()

function(build_pixel_shader_spec_constants TARGETOBJ SHADERNAME)
    build_shader_spirv(${TARGETOBJ} ${SHADERNAME} "${DXC_PS_OPTS}" ${ARGN})
    if (APPLE)
        build_shader_msl(${TARGETOBJ} ${SHADERNAME} "${DXC_PS_OPTS}" ${ARGN})
    endif()
endfunction()

function(build_vertex_shader_spec_constants TARGETOBJ SHADERNAME)
    build_shader_spirv(${TARGETOBJ} ${SHADERNAME} "${DXC_VS_OPTS}" ${ARGN})
    if (APPLE)
        build_shader_msl(${TARGETOBJ} ${SHADERNAME} "${DXC_VS_OPTS}" ${ARGN})
    endif()
endfunction()

function(build_compute_shader TARGETOBJ SHADERNAME)
    build_shader(${TARGETOBJ} ${SHADERNAME} "${DXC_CS_OPTS}" ${ARGN})
endfunction()

function(build_ray_shader TARGETOBJ SHADERNAME)
    build_shader(${TARGETOBJ} ${SHADERNAME} "${DXC_RT_OPTS}" ${ARGN})
endfunction()

# Point cmake at src/contrib/mupen64plus-win32-deps/SDL2-2.26.3 for SDL2 on windows, look for an installed package on other systems
if (WIN32)
    set(SDL2_INCLUDE_DIRS "${PROJECT_SOURCE_DIR}/src/contrib/mupen64plus-win32-deps/SDL2-2.26.3/include")
    set(SDL2_LIBRARIES "SDL2" "SDL2main")
    link_directories("${PROJECT_SOURCE_DIR}/src/contrib/mupen64plus-win32-deps/SDL2-2.26.3/lib/x64")
elseif (ANDROID)
    # SDL2 is consumed via headers only on Android; the runtime SDL2 lib is
    # provided by the APK (SDLActivity). Point at a host-side SDL2 headers tree
    # for the spike. Override with -DSDL2_INCLUDE_DIRS=... if needed.
    if (NOT SDL2_INCLUDE_DIRS)
        set(SDL2_INCLUDE_DIRS "/opt/homebrew/include/SDL2")
    endif()
    set(SDL2_LIBRARIES "")
else()
    find_package(SDL2 REQUIRED)
endif()

message(STATUS "${SDL2_INCLUDE_DIRS} ${SDL2_LIBRARIES}")

add_compile_definitions(
    HLSL_CPU
    FFX_GCC
    IMGUI_IMPL_VULKAN_NO_PROTOTYPES
)

if ((CMAKE_SYSTEM_NAME MATCHES "Linux" OR ANDROID) AND RT64_SDL_WINDOW_VULKAN)
    add_compile_definitions("PLUME_SDL_VULKAN_ENABLED")
    add_compile_definitions("RT64_SDL_WINDOW_VULKAN")
endif()

set (SOURCES
    "${PROJECT_SOURCE_DIR}/src/common/rt64_common.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_dynamic_libraries.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_elapsed_timer.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_emulator_configuration.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_enhancement_configuration.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_filesystem_zip.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_load_types.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_mapped_file.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_math.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_profiling_timer.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_replacement_database.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_thread.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_timer.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_user_configuration.cpp"
    "${PROJECT_SOURCE_DIR}/src/common/rt64_user_paths.cpp"

    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3d.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3dex.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3dex2.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3dgolden.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3dpd.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3dwave.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_f3dzex2.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_l3dex2.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_rdp.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_s2dex.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_s2dex2.cpp"
    "${PROJECT_SOURCE_DIR}/src/gbi/rt64_gbi_extended.cpp"

    "${PROJECT_SOURCE_DIR}/src/gui/rt64_camera_controller.cpp"
    "${PROJECT_SOURCE_DIR}/src/gui/rt64_debugger_inspector.cpp"
    "${PROJECT_SOURCE_DIR}/src/gui/rt64_file_dialog.cpp"
    "${PROJECT_SOURCE_DIR}/src/gui/rt64_inspector.cpp"

    "${PROJECT_SOURCE_DIR}/src/hle/rt64_application.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_application_window.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_color_converter.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_command_warning.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_draw_call.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_framebuffer.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_framebuffer_changes.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_framebuffer_manager.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_framebuffer_pair.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_framebuffer_storage.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_game_frame.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_interpreter.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_light_manager.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_present_queue.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_projection.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_rdp.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_rdp_tmem.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_rigid_body.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_rsp.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_state.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_vi.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_workload.cpp"
    "${PROJECT_SOURCE_DIR}/src/hle/rt64_workload_queue.cpp"

    "${PROJECT_SOURCE_DIR}/src/imgui/imgui_impl_sdl2_custom.cpp"

    "${PROJECT_SOURCE_DIR}/src/render/rt64_buffer_uploader.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_framebuffer_renderer.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_geometry_mode.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_look_at_processor.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_native_target.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_optimus.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_projection_processor.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_raster_shader.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_raster_shader_cache.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_render_target.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_render_target_manager.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_render_worker.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_rsp_processor.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_shader_common.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_shader_compiler.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_shader_library.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_texture_cache.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_tile_processor.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_transform_processor.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_upscaler.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_vertex_processor.cpp"
    "${PROJECT_SOURCE_DIR}/src/render/rt64_vi_renderer.cpp"

    "${PROJECT_SOURCE_DIR}/src/preset/rt64_preset.cpp"
    "${PROJECT_SOURCE_DIR}/src/preset/rt64_preset_draw_call.cpp"
    "${PROJECT_SOURCE_DIR}/src/preset/rt64_preset_light.cpp"
    "${PROJECT_SOURCE_DIR}/src/preset/rt64_preset_material.cpp"
    "${PROJECT_SOURCE_DIR}/src/preset/rt64_preset_scene.cpp"

    "${PROJECT_SOURCE_DIR}/src/shared/rt64_hlsl_json.cpp"

    "${PROJECT_SOURCE_DIR}/src/rhi/rt64_render_hooks.cpp"

    "${PROJECT_SOURCE_DIR}/src/contrib/imgui/imgui.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/imgui/imgui_demo.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/imgui/imgui_draw.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/imgui/imgui_tables.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/imgui/imgui_widgets.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/imgui/backends/imgui_impl_vulkan.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/im3d/im3d.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/implot/implot.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/implot/implot_demo.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/implot/implot_items.cpp"
    "${PROJECT_SOURCE_DIR}/src/contrib/miniz/miniz.c"
)

include_directories(
    "${SDL2_INCLUDE_DIRS}"
    "${PROJECT_SOURCE_DIR}/src"
    "${PROJECT_SOURCE_DIR}/src/contrib"
    "${PROJECT_SOURCE_DIR}/src/contrib/plume"
    "${PROJECT_SOURCE_DIR}/src/contrib/imgui"
    "${PROJECT_SOURCE_DIR}/src/contrib/hlslpp/include"
    "${PROJECT_SOURCE_DIR}/src/contrib/mupen64plus-core/src/api"
    "${PROJECT_SOURCE_DIR}/src/contrib/nativefiledialog-extended/src/include"
)

if (WIN32)
    # DXC only needs to be included during runtime for Windows.
    include_directories("${PROJECT_SOURCE_DIR}/src/contrib/dxc/inc")
endif()

if (${RT64_STATIC})
    add_library(rt64 STATIC ${SOURCES})
else()
    add_library(rt64 SHARED ${SOURCES})
endif()

set_target_properties(rt64 PROPERTIES OUTPUT_NAME "rt64")
set_target_properties(rt64 PROPERTIES PREFIX "")

# Add common libraries.
if (NOT RT64_ANDROID_SPIKE OR ANDROID)
    target_link_libraries(rt64 re-spirv)
endif()
if (NOT RT64_ANDROID_SPIKE)
    target_link_libraries(rt64 nfd)
endif()
target_link_libraries(rt64 libzstd_static)
target_link_libraries(rt64 plume)

# Add tools.
if (NOT RT64_ANDROID_SPIKE)
    add_subdirectory(src/tools/texture_hasher)
    add_subdirectory(src/tools/texture_packer)
endif()

# Add any Apple-specific source files and libraries
if (APPLE)
    add_subdirectory(src/tools/spirv_cross_msl)

    target_sources(rt64 PRIVATE
        "${PROJECT_SOURCE_DIR}/src/apple/rt64_apple.mm"
    )
    target_include_directories(rt64 PRIVATE "${PROJECT_SOURCE_DIR}/src/contrib/metal-cpp")
    target_link_libraries(rt64 "-framework Metal -framework QuartzCore -framework CoreGraphics -framework IOKit")
endif()

# Add any Windows-specific source files and libraries
if (WIN32)
    target_sources(rt64 PRIVATE
        "${PROJECT_SOURCE_DIR}/src/contrib/imgui/backends/imgui_impl_dx12.cpp"
        "${PROJECT_SOURCE_DIR}/src/contrib/imgui/backends/imgui_impl_win32.cpp"
    )
    target_link_libraries(rt64
        delayimp.lib
        D3D12
        DXGI
        Shcore.lib
        ${PROJECT_SOURCE_DIR}/src/contrib/dxc/lib/x64/dxcompiler.lib)

    target_include_directories(rt64 PRIVATE ${zstd_SOURCE_DIR}/lib)
endif()

target_link_directories(rt64 PRIVATE ${PROJECT_SOURCE_DIR}/src/contrib/dxc)

if (NOT ANDROID)
    target_link_libraries(rt64 ${SDL2_LIBRARIES})
endif()

if (CMAKE_SYSTEM_NAME MATCHES "Linux")
    if (NOT RT64_SDL_WINDOW_VULKAN)
        find_package(X11 REQUIRED)
        target_include_directories(rt64 PUBLIC ${X11_INCLUDE_DIR} ${X11_Xrandr_INCLUDE_PATH})
        target_link_libraries(rt64 ${X11_LIBRARIES} ${X11_Xrandr_LIB})
    endif()
endif()

preprocess_shader(rt64 "src/shaders/RenderParams.hlsli")

if (${WIN32})
    build_library_shader(rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSLibrary.hlsl")
    build_library_shader(rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSLibraryMS.hlsl" "-D MULTISAMPLING")
    build_library_shader(rt64 "src/shaders/RasterVS.hlsl" "src/shaders/RasterVSLibrary.hlsl")
endif()

build_pixel_shader(  rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSDynamic.hlsl" "-D DYNAMIC_RENDER_PARAMS" ${RT64_RASTER_PS_EXTRA_DXC} ${RT64_DYNAMIC_PS_EXTRA_DXC})
build_pixel_shader(  rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSDynamicMS.hlsl" "-D DYNAMIC_RENDER_PARAMS" "-D MULTISAMPLING" ${RT64_RASTER_PS_EXTRA_DXC} ${RT64_DYNAMIC_PS_EXTRA_DXC})
build_pixel_shader_spec_constants(rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSSpecConstant.hlsl" "-D SPEC_CONSTANT_RENDER_PARAMS" ${RT64_RASTER_PS_EXTRA_DXC} ${RT64_SPEC_CONST_PS_EXTRA_DXC})
build_pixel_shader_spec_constants(rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSSpecConstantMS.hlsl" "-D SPEC_CONSTANT_RENDER_PARAMS" "-D MULTISAMPLING" ${RT64_RASTER_PS_EXTRA_DXC} ${RT64_SPEC_CONST_PS_EXTRA_DXC})
build_pixel_shader_spec_constants(rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSSpecConstantFlat.hlsl" "-D SPEC_CONSTANT_RENDER_PARAMS" "-D VERTEX_FLAT_COLOR" ${RT64_RASTER_PS_EXTRA_DXC} ${RT64_SPEC_CONST_PS_EXTRA_DXC})
build_pixel_shader_spec_constants(rt64 "src/shaders/RasterPS.hlsl" "src/shaders/RasterPSSpecConstantFlatMS.hlsl" "-D SPEC_CONSTANT_RENDER_PARAMS" "-D VERTEX_FLAT_COLOR" "-D MULTISAMPLING" ${RT64_RASTER_PS_EXTRA_DXC} ${RT64_SPEC_CONST_PS_EXTRA_DXC})
build_vertex_shader( rt64 "src/shaders/RasterVS.hlsl" "src/shaders/RasterVSDynamic.hlsl" "-D DYNAMIC_RENDER_PARAMS")
build_vertex_shader_spec_constants( rt64 "src/shaders/RasterVS.hlsl" "src/shaders/RasterVSSpecConstant.hlsl" "-D SPEC_CONSTANT_RENDER_PARAMS")
build_vertex_shader_spec_constants( rt64 "src/shaders/RasterVS.hlsl" "src/shaders/RasterVSSpecConstantFlat.hlsl" "-D SPEC_CONSTANT_RENDER_PARAMS" "-D VERTEX_FLAT_COLOR")
build_compute_shader(rt64 "src/shaders/FbChangesClearCS.hlsl")
build_pixel_shader(  rt64 "src/shaders/FbChangesDrawColorPS.hlsl")
build_pixel_shader(  rt64 "src/shaders/FbChangesDrawDepthPS.hlsl")
build_compute_shader(rt64 "src/shaders/FbReadAnyChangesCS.hlsl" "src/shaders/FbReadAnyChangesCS.hlsl" "-O0" "--strip-image-format-unknown")
build_compute_shader(rt64 "src/shaders/FbReinterpretCS.hlsl" "src/shaders/FbReinterpretCS.hlsl" "-O0")
build_compute_shader(rt64 "src/shaders/FbReadAnyFullCS.hlsl" "src/shaders/FbReadAnyFullCS.hlsl" "-O0" "--strip-image-format-unknown")
build_compute_shader(rt64 "src/shaders/FbWriteColorCS.hlsl" "src/shaders/FbWriteColorCS.hlsl" "-O0" "--strip-image-format-unknown")
build_compute_shader(rt64 "src/shaders/FbWriteDepthCS.hlsl" "src/shaders/FbWriteDepthCS.hlsl" "--strip-image-format-unknown")
build_compute_shader(rt64 "src/shaders/FbWriteDepthCS.hlsl" "src/shaders/FbWriteDepthCSMS.hlsl" "-D MULTISAMPLING" "--strip-image-format-unknown")
build_compute_shader(rt64 "src/shaders/GaussianFilterRGB3x3CS.hlsl")
build_compute_shader(rt64 "src/shaders/BoxFilterCS.hlsl")
build_compute_shader(rt64 "src/shaders/BicubicScalingCS.hlsl")
build_compute_shader(rt64 "src/shaders/HistogramAverageCS.hlsl")
build_compute_shader(rt64 "src/shaders/HistogramClearCS.hlsl")
build_compute_shader(rt64 "src/shaders/HistogramSetCS.hlsl")
build_compute_shader(rt64 "src/shaders/IdleCS.hlsl")
build_compute_shader(rt64 "src/shaders/LuminanceHistogramCS.hlsl")
build_pixel_shader(  rt64 "src/shaders/PostBlendDitherNoisePS.hlsl" "src/shaders/PostBlendDitherNoiseAddPS.hlsl" "-D ADD_MODE")
build_pixel_shader(  rt64 "src/shaders/PostBlendDitherNoisePS.hlsl" "src/shaders/PostBlendDitherNoiseSubPS.hlsl" "-D SUB_MODE")
build_pixel_shader(  rt64 "src/shaders/PostBlendDitherNoisePS.hlsl" "src/shaders/PostBlendDitherNoiseSubNegativePS.hlsl" "-D SUB_MODE" "-D NEGATIVE_MODE")
build_compute_shader(rt64 "src/shaders/RSPModifyCS.hlsl")
build_compute_shader(rt64 "src/shaders/RSPProcessCS.hlsl")
build_compute_shader(rt64 "src/shaders/RSPWorldCS.hlsl")
build_compute_shader(rt64 "src/shaders/RSPSmoothNormalCS.hlsl")
build_compute_shader(rt64 "src/shaders/RSPVertexTestZCS.hlsl")
build_compute_shader(rt64 "src/shaders/RSPVertexTestZCS.hlsl" "src/shaders/RSPVertexTestZCSMS.hlsl" "-D MULTISAMPLING")
build_pixel_shader(  rt64 "src/shaders/RtCopyColorToDepthPS.hlsl")
build_pixel_shader(  rt64 "src/shaders/RtCopyColorToDepthPS.hlsl" "src/shaders/RtCopyColorToDepth2XPS.hlsl" "-D SAMPLES_2X")
build_pixel_shader(  rt64 "src/shaders/RtCopyColorToDepthPS.hlsl" "src/shaders/RtCopyColorToDepth4XPS.hlsl" "-D SAMPLES_4X")
build_pixel_shader(  rt64 "src/shaders/RtCopyColorToDepthPS.hlsl" "src/shaders/RtCopyColorToDepth8XPS.hlsl" "-D SAMPLES_8X")
build_pixel_shader(  rt64 "src/shaders/RtCopyDepthToColorPS.hlsl")
build_pixel_shader(  rt64 "src/shaders/RtCopyDepthToColorPS.hlsl" "src/shaders/RtCopyDepthToColor2XPS.hlsl" "-D SAMPLES_2X")
build_pixel_shader(  rt64 "src/shaders/RtCopyDepthToColorPS.hlsl" "src/shaders/RtCopyDepthToColor4XPS.hlsl" "-D SAMPLES_4X")
build_pixel_shader(  rt64 "src/shaders/RtCopyDepthToColorPS.hlsl" "src/shaders/RtCopyDepthToColor8XPS.hlsl" "-D SAMPLES_8X")
build_pixel_shader(  rt64 "src/shaders/TextureCopyPS.hlsl")
build_compute_shader(rt64 "src/shaders/TextureDecodeCS.hlsl")
build_pixel_shader(  rt64 "src/shaders/TextureResolvePS.hlsl" "src/shaders/TextureResolveSamples2XPS.hlsl" "-D SAMPLES_2X")
build_pixel_shader(  rt64 "src/shaders/TextureResolvePS.hlsl" "src/shaders/TextureResolveSamples4XPS.hlsl" "-D SAMPLES_4X")
build_pixel_shader(  rt64 "src/shaders/TextureResolvePS.hlsl" "src/shaders/TextureResolveSamples8XPS.hlsl" "-D SAMPLES_8X")
build_pixel_shader(  rt64 "src/shaders/VideoInterfacePS.hlsl" "src/shaders/VideoInterfacePSRegular.hlsl" ${RT64_VI_EXTRA_DXC})
build_pixel_shader(  rt64 "src/shaders/VideoInterfacePS.hlsl" "src/shaders/VideoInterfacePSPixel.hlsl" "-D PIXEL_ANTIALIASING" ${RT64_VI_EXTRA_DXC})
build_vertex_shader( rt64 "src/shaders/FullScreenVS.hlsl")
build_vertex_shader( rt64 "src/shaders/Im3DVS.hlsl")
build_pixel_shader(  rt64 "src/shaders/ComposePS.hlsl")
build_pixel_shader(  rt64 "src/shaders/DebugPS.hlsl")
build_pixel_shader(  rt64 "src/shaders/Im3DPS.hlsl")
build_pixel_shader(  rt64 "src/shaders/PostProcessPS.hlsl")

target_include_directories(rt64 PRIVATE ${CMAKE_BINARY_DIR}/src)

