// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

#include "math_utils.h"
#include "shape.h"
#include <iostream>
#include <memory>
#include <vector>

using namespace std;

void testMathUtils() {
  cout << "\n=== Testing Math Utils ===" << endl;

  // Test addition
  int sum = MathUtils::add(5, 3);
  cout << "5 + 3 = " << sum << endl;

  // Test multiplication
  int product = MathUtils::multiply(4, 7);
  cout << "4 * 7 = " << product << endl;

  // Test factorial
  long long fact = MathUtils::factorial(10);
  cout << "10! = " << fact << endl;

  // Test prime checking
  int num = 17;
  bool prime = MathUtils::isPrime(num);
  cout << num << " is " << (prime ? "prime" : "not prime") << endl;
}

void testShapes() {
  cout << "\n=== Testing Shapes ===" << endl;

  // Create shapes using smart pointers
  vector<unique_ptr<Shape>> shapes;
  shapes.push_back(make_unique<Rectangle>(5.0, 3.0));
  shapes.push_back(make_unique<Circle>(4.0));

  // Test polymorphism
  for (const auto &shape : shapes) {
    cout << "\n" << shape->getName() << ":" << endl;
    cout << "  Area: " << shape->area() << endl;
    cout << "  Perimeter: " << shape->perimeter() << endl;
  }

  // Test specific methods
  Rectangle rect(10.0, 5.0);
  cout << "\nRectangle dimensions: " << rect.getWidth() << " x "
       << rect.getHeight() << endl;

  Circle circle(7.0);
  cout << "Circle radius: " << circle.getRadius() << endl;
}

int main() {
  cout << "C++ SCIP Test Program" << endl;
  cout << "=====================" << endl;

  testMathUtils();
  testShapes();

  cout << "\n=== All tests completed ===" << endl;
  return 0;
}
