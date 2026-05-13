/*
 * SPDX-FileCopyrightText: 2025-2026 CodeMiner Contributors
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#ifndef SHAPE_H
#define SHAPE_H

/**
 * Base class for geometric shapes
 */
class Shape {
public:
  virtual ~Shape() = default;

  /**
   * Calculate the area of the shape
   * @return Area of the shape
   */
  virtual double area() const = 0;

  /**
   * Calculate the perimeter of the shape
   * @return Perimeter of the shape
   */
  virtual double perimeter() const = 0;

  /**
   * Get the name of the shape
   * @return Name of the shape
   */
  virtual const char *getName() const = 0;
};

/**
 * Rectangle class
 */
class Rectangle : public Shape {
private:
  double width;
  double height;

public:
  Rectangle(double w, double h);

  double area() const override;
  double perimeter() const override;
  const char *getName() const override;

  double getWidth() const { return width; }
  double getHeight() const { return height; }
};

/**
 * Circle class
 */
class Circle : public Shape {
private:
  double radius;
  static constexpr double PI = 3.14159265358979323846;

public:
  Circle(double r);

  double area() const override;
  double perimeter() const override;
  const char *getName() const override;

  double getRadius() const { return radius; }
};

#endif // SHAPE_H
