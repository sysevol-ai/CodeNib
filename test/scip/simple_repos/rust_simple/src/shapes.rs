// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/// Shape module providing geometry abstractions
use std::f64::consts::PI;

/// Base trait for all shapes
pub trait Shape {
    /// Calculate the area of the shape
    fn area(&self) -> f64;

    /// Calculate the perimeter of the shape
    fn perimeter(&self) -> f64;

    /// Get the name of the shape
    fn name(&self) -> &str;
}

/// Rectangle shape with width and height
pub struct Rectangle {
    width: f64,
    height: f64,
}

impl Rectangle {
    /// Create a new rectangle
    pub fn new(width: f64, height: f64) -> Self {
        Rectangle { width, height }
    }

    /// Get the width of the rectangle
    pub fn width(&self) -> f64 {
        self.width
    }

    /// Get the height of the rectangle
    pub fn height(&self) -> f64 {
        self.height
    }
}

impl Shape for Rectangle {
    fn area(&self) -> f64 {
        self.width * self.height
    }

    fn perimeter(&self) -> f64 {
        2.0 * (self.width + self.height)
    }

    fn name(&self) -> &str {
        "Rectangle"
    }
}

/// Circle shape with radius
pub struct Circle {
    radius: f64,
}

impl Circle {
    /// Create a new circle
    pub fn new(radius: f64) -> Self {
        Circle { radius }
    }

    /// Get the radius of the circle
    pub fn radius(&self) -> f64 {
        self.radius
    }
}

impl Shape for Circle {
    fn area(&self) -> f64 {
        PI * self.radius * self.radius
    }

    fn perimeter(&self) -> f64 {
        2.0 * PI * self.radius
    }

    fn name(&self) -> &str {
        "Circle"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_rectangle() {
        let rect = Rectangle::new(5.0, 3.0);
        assert_eq!(rect.width(), 5.0);
        assert_eq!(rect.height(), 3.0);
        assert_eq!(rect.area(), 15.0);
        assert_eq!(rect.perimeter(), 16.0);
        assert_eq!(rect.name(), "Rectangle");
    }

    #[test]
    fn test_circle() {
        let circle = Circle::new(4.0);
        assert_eq!(circle.radius(), 4.0);
        assert!((circle.area() - 50.265482).abs() < 0.0001);
        assert!((circle.perimeter() - 25.132741).abs() < 0.0001);
        assert_eq!(circle.name(), "Circle");
    }
}
