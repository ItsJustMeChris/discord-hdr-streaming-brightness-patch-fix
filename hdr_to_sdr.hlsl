// HDR -> SDR conversion that replaces Discord's two HDR screen-share pixel shaders.
// The bindings, cbuffer layout and input signature must match Discord's originals.

#ifndef WHITE_NITS
#define WHITE_NITS 480.0   // HDR brightness (nits) that becomes SDR white
#endif

Texture2D<float4> sample_texture : register(t0);
SamplerState sample_type : register(s0);

cbuffer Constant : register(b0) {
    float scale;               // Discord's brightness multiplier, deliberately ignored
    bool use_source_alpha;
    bool needs_srgb_conversion;
    bool needs_st2084;
};

static const float SDR_SCALE = 80.0 / WHITE_NITS;   // scRGB 1.0 == 80 nits

float3 pq_to_linear(float3 v) {                      // ST.2084 -> nits / 10000
    const float m1 = 0.1593017578125, m2 = 78.84375;
    const float c1 = 0.8359375, c2 = 18.8515625, c3 = 18.6875;
    float3 p = pow(abs(v), 1.0 / m2);
    return pow(max(p - c1, 0.0) / (c2 - c3 * p), 1.0 / m1);
}

float3 srgb_oetf(float3 c) {
    float3 lo = c * 12.92;
    float3 hi = 1.055 * pow(c, 1.0 / 2.4) - 0.055;
    return lerp(lo, hi, step(0.0031308, c));
}

float4 main(float4 pos : SV_POSITION, float2 uv : TEXCOORD0) : SV_TARGET {
    float4 c = sample_texture.Sample(sample_type, uv);
    float3 rgb = c.rgb;
    if (needs_st2084) {
        float3 lin = pq_to_linear(rgb) * 125.0;      // 10000 / 80 -> scRGB units
        rgb = float3(dot(float3( 1.660491, -0.587641, -0.072850), lin),   // BT.2020 -> BT.709
                     dot(float3(-0.124550,  1.132900, -0.008349), lin),
                     dot(float3(-0.018151, -0.100579,  1.118730), lin));
    }
    rgb = max(rgb * SDR_SCALE, 0.0);
    rgb /= max(max(rgb.r, max(rgb.g, rgb.b)), 1.0); // hue-preserving clip
    if (needs_srgb_conversion)
        rgb = srgb_oetf(rgb);
    return use_source_alpha ? float4(rgb, saturate(c.a)) : float4(rgb, 1.0);
}
