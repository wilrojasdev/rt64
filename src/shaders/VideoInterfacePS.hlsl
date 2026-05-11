//
// RT64
//

#include "shared/rt64_video_interface.h"

[[vk::push_constant]] ConstantBuffer<VideoInterfaceCB> gConstants : register(b0);
Texture2D<float4> gInput : register(t1);
SamplerState gSampler : register(s2);

// Limit texture sampling to the area the VI can sample of the texture.

float4 SampleInput(float2 uv) {
    const float2 LowerRight = gConstants.videoResolution / gConstants.textureResolution;
    const float2 HalfPixel = float2(0.5f, 0.5f) / gConstants.textureResolution;
    float2 outsideBorder = step(LowerRight, uv);
    float4 sampledColor = gInput.SampleLevel(gSampler, clamp(uv, HalfPixel, LowerRight - HalfPixel), 0);
    float4 gammaCorrectedColor = pow(sampledColor, gConstants.gamma);
    gammaCorrectedColor.rgb *= max(1.0f - outsideBorder.x - outsideBorder.y, 0.0f);
    gammaCorrectedColor.a = 1.0f;
    return gammaCorrectedColor;
}

//
// Sourced from https://www.shadertoy.com/view/csX3RH
//
float4 PixelAntialiasing(float2 uv) {
    float2 uvTexspace = uv * gConstants.videoResolution;
    float2 seam = floor(uvTexspace + 0.5f);
    uvTexspace = (uvTexspace - seam) / fwidth(uvTexspace) + seam;
    uvTexspace = clamp(uvTexspace, seam - 0.5f, seam + 0.5f);
    return SampleInput(uvTexspace / gConstants.textureResolution);
}


float4 PSMain(in float4 pos : SV_Position, in float2 uv : TEXCOORD0) : SV_TARGET {
    // CMake RT64_DIAG_VI_MODE → DXC -D (see lib/rt64/CMakeLists.txt).
    //   1 = solid cyan (proves VI PS reaches swapchain).
    //   2 = raw sample of gInput, no AA / no gamma / no border clamp (shows what the
    //       colorTarget VI samples actually contains — white = ~1.0/undefined, black
    //       = cleared/zero, anything else = real raster output landed where expected).
    //   3 = UV grid (red=u, green=v) — proves the PS executes per-pixel and has valid
    //       UVs, independent of any sampling.
    //   4 = channel classifier of gInput sample. Decides per-pixel from a single sample:
    //       red    → all channels > 0.9 (undefined memory / saturated white from Mali)
    //       green  → all channels < 0.05 (texture is genuinely zero — clear works,
    //                raster never wrote here, identity divergence confirmed)
    //       blue   → anything in between (real game content)
    //       black  → mixed extremes (some saturated, some zero — partial write?)
#if defined(RT64_DIAG_VI_MODE)
#if RT64_DIAG_VI_MODE == 1
    return float4(0.0f, 1.0f, 1.0f, 1.0f);
#elif RT64_DIAG_VI_MODE == 2
    return float4(gInput.SampleLevel(gSampler, uv, 0).rgb, 1.0f);
#elif RT64_DIAG_VI_MODE == 3
    return float4(uv.x, uv.y, 0.0f, 1.0f);
#elif RT64_DIAG_VI_MODE == 4
    {
        float3 s = gInput.SampleLevel(gSampler, uv, 0).rgb;
        bool allHigh = (s.r > 0.9f) && (s.g > 0.9f) && (s.b > 0.9f);
        bool allLow  = (s.r < 0.05f) && (s.g < 0.05f) && (s.b < 0.05f);
        if (allHigh) return float4(1.0f, 0.0f, 0.0f, 1.0f); // red = saturated white
        if (allLow)  return float4(0.0f, 1.0f, 0.0f, 1.0f); // green = zeroed
        return float4(0.0f, 0.0f, 1.0f, 1.0f);              // blue = real content
    }
#endif
#endif
#ifdef PIXEL_ANTIALIASING
    return PixelAntialiasing(uv);
#else
    return SampleInput((uv / gConstants.textureResolution) * gConstants.videoResolution);
#endif
}