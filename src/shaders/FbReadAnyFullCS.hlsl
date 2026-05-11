//
// RT64
//

#include "Depth.hlsli"
#include "FbCommon.hlsli"

[[vk::push_constant]] ConstantBuffer<FbCommonCB> gConstants : register(b0, space0);
Buffer<uint> gNewInput : register(t1, space0);
// See FbReadAnyChangesCS for the rationale; format-unknown so storage
// image aliasing across framebuffer formats works under strict drivers.
[[vk::image_format("unknown")]] RWTexture2D<float4> gOutputChangeColor : register(u0, space1);
// See FbReadAnyChangesCS for why gOutputChangeDepth is float4.
[[vk::image_format("unknown")]] RWTexture2D<float4> gOutputChangeDepth : register(u1, space1);
[[vk::image_format("unknown")]] RWTexture2D<uint>   gOutputChangeBoolean : register(u2, space1);

[numthreads(FB_COMMON_WORKGROUP_SIZE, FB_COMMON_WORKGROUP_SIZE, 1)]
void CSMain(uint2 coord : SV_DispatchThreadID) {
    if ((coord.x < gConstants.resolution.x) && (coord.y < gConstants.resolution.y)) {
        uint bufferIndex = coord.y * gConstants.resolution.x + coord.x;
        uint2 pixelCoord = gConstants.offset + coord.xy;
        uint swappedUint = EndianSwapUINT(gNewInput[bufferIndex], gConstants.siz);
        if (gConstants.fmt == G_IM_FMT_DEPTH) {
            const float newDepth = Depth16ToFloat(swappedUint);
            gOutputChangeDepth[pixelCoord] = float4(newDepth, newDepth, newDepth, newDepth);
        }
        else {
            const float4 newColor = UINTToFloat4(swappedUint, gConstants.siz, gConstants.fmt);
            gOutputChangeColor[pixelCoord] = newColor;
        }

        gOutputChangeBoolean[pixelCoord] = 1;
    }
}