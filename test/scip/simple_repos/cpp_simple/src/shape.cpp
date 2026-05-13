// SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "shape.h"

// Rectangle implementation
Rectangle::Rectangle(double w, double h) : width(w), height(h) {}

double Rectangle::area() const { return width * height; }

double Rectangle::perimeter() const { return 2 * (width + height); }

const char *Rectangle::getName() const { return "Rectangle"; }

// Circle implementation
Circle::Circle(double r) : radius(r) {}

double Circle::area() const { return PI * radius * radius; }

double Circle::perimeter() const { return 2 * PI * radius; }

const char *Circle::getName() const { return "Circle"; }
