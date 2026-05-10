//
// RT64
//

#include "TextureDecoder.hlsli"

#define GROUP_SIZE 8

struct TextureDecodeCB {
    uint2 Resolution;
    uint fmt;
    uint siz;
    uint address;
    uint stride;
    uint tlut;
    uint palette;
};

[[vk::push_constant]] ConstantBuffer<TextureDecodeCB> gConstants : register(b0);
Texture1D<uint> TMEM : register(t1);
// Tag the storage image as rgba8 so the SPIR-V's OpTypeImage Format
// operand matches the runtime VkImageView (R8G8B8A8_UNORM). Without this
// DXC emits Rgba32f, which produces undefined values on drivers that
// validate format aliasing strictly (Mali Valhall observed in Phase 11
// — surface presented a fully white frame instead of the decoded
// texture). The shader already feeds normalized [0,1] floats from
// sampleTMEM/RGBA32ToFloat4, so unorm rgba8 is the correct semantic.
[[vk::image_format("rgba8")]]
RWTexture2D<float4> RGBA32 : register(u2);

[numthreads(GROUP_SIZE, GROUP_SIZE, 1)]
void CSMain(uint2 coord : SV_DispatchThreadID) {
    if ((coord.x < gConstants.Resolution.x) && (coord.y < gConstants.Resolution.y)) {
        RGBA32[coord] = sampleTMEM(coord, gConstants.siz, gConstants.fmt, gConstants.address, gConstants.stride, gConstants.tlut, gConstants.palette, TMEM);
    }
}