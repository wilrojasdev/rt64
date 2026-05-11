//
// RT64
//

#pragma once

#include <stdint.h>

#include "common/rt64_common.h"
#include "hle/rt64_framebuffer.h"
#include "hle/rt64_framebuffer_changes.h"

#include "rt64_descriptor_sets.h"
#include "rt64_render_worker.h"

namespace RT64 {
    struct RenderWorker;

    struct RenderTarget {
        static const long MaxDimension;

        std::unique_ptr<RenderTexture> texture;
        std::unique_ptr<RenderTexture> resolvedTexture;
        std::unique_ptr<RenderTextureView> textureView;
        std::unique_ptr<RenderTextureView> resolvedTextureView;
        std::unique_ptr<RenderTexture> downsampledTexture;
        uint32_t downsampledTextureMultiplier = 0;
        std::unique_ptr<RenderTexture> dummyTexture;
        std::unique_ptr<RenderFramebuffer> textureFramebuffer;
        std::unique_ptr<RenderFramebuffer> resolveFramebuffer;
        RenderMultisampling multisampling;
        RenderFormat format = RenderFormat::UNKNOWN;
        std::unique_ptr<RenderTargetCopyDescriptorSet> targetCopyDescSet;
        std::unique_ptr<TextureCopyDescriptorSet> textureCopyDescSet;
        std::unique_ptr<TextureCopyDescriptorSet> textureResolveDescSet;
        std::unique_ptr<BoxFilterDescriptorSet> filterDescSet;
        std::unique_ptr<FramebufferWriteDescriptorTextureSet> fbWriteDescSet;
        uint32_t addressForName = 0;
        uint32_t width = 0;
        uint32_t height = 0;
        uint64_t textureRevision = 0;
        Framebuffer::Type type = Framebuffer::Type::None;
        hlslpp::float2 resolutionScale = { 1.0f, 1.0f };
        uint32_t downsampleMultiplier = 1;
        int32_t misalignX = 0;
        int32_t invMisalignX = 0;
        bool resolvedTextureDirty = false;
        bool usesHDR = false;
        // True between setupColor()/setupDepth() and the first time the
        // texture is actually written. Strict drivers (Mali Valhall G57) leave
        // VkImage memory at ~1.0 in every channel until written, so we issue
        // a one-shot clear when the next render-pass-bound operation runs.
        bool needsInitialClear = false;

        RenderTarget(uint32_t addressForName, Framebuffer::Type type, const RenderMultisampling &multisampling, bool usesHDR);
        ~RenderTarget();
        void releaseTextures();
        bool resize(RenderWorker *worker, uint32_t newWidth, uint32_t newHeight);
        void setupColor(RenderWorker *worker, uint32_t width, uint32_t height);
        void setupDepth(RenderWorker *worker, uint32_t width, uint32_t height);
        void setupDummy(RenderWorker *worker);
        void setupColorFramebuffer(RenderWorker *worker);
        void setupResolveFramebuffer(RenderWorker *worker);
        void setupDepthFramebuffer(RenderWorker *worker);
        void copyFromTarget(RenderWorker *worker, RenderTarget *src, uint32_t x, uint32_t y, uint32_t width, uint32_t height, const ShaderLibrary *shaderLibrary);
        void resolveFromTarget(RenderWorker *worker, RenderTarget *src, const ShaderLibrary *shaderLibrary);
        void copyFromChanges(RenderWorker *worker, const FramebufferChange &fbChange, uint32_t fbWidth, uint32_t fbHeight, uint32_t rowStart, const ShaderLibrary *shaderLibrary);
        void clearColorTarget(RenderWorker *worker);
        void clearDepthTarget(RenderWorker *worker);
        // Phase 12: drains the deferred initial clear set by setupColor /
        // setupDepth. No-op once consumed. Call before any pass that reads
        // from this target's texture so strict drivers (Mali Valhall G57)
        // don't return ~1.0 from VkImage memory that's still UNDEFINED.
        void drainInitialClear(RenderWorker *worker);
        void downsampleTarget(RenderWorker *worker, const ShaderLibrary *shaderLibrary);
        void resolveTarget(RenderWorker *worker, const ShaderLibrary *shaderLibrary);
        void recordRasterResolve(RenderWorker *worker, const TextureCopyDescriptorSet *srcDescriptorSet, uint32_t x, uint32_t y, uint32_t width, uint32_t height, const ShaderLibrary *shaderLibrary);
        void markForResolve();
        bool usesResolve() const;
        RenderTexture *getResolvedTexture() const;
        RenderTextureView *getResolvedTextureView() const;
        bool isEmpty() const;
        static void computeScaledSize(uint32_t nativeWidth, uint32_t nativeHeight, hlslpp::float2 resolutionScale, uint32_t &scaledWidth, uint32_t &scaledHeight, uint32_t &misalignmentX);
        static hlslpp::float2 computeFixedResolutionScale(uint32_t nativeWidth, hlslpp::float2 resolutionScale);
        static RenderFormat colorBufferFormat(bool usesHDR);
        static RenderFormat depthBufferFormat();
    };
};