// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

#ifndef PALACE_UTILS_SURFACE_MASK_HPP
#define PALACE_UTILS_SURFACE_MASK_HPP

#include <array>

namespace palace
{

// Original selected-patch perimeter geometry, retained independently of cracked mesh IDs.
struct SurfaceMaskEdge
{
  std::array<double, 3> a;
  std::array<double, 3> b;
};

}  // namespace palace

#endif  // PALACE_UTILS_SURFACE_MASK_HPP
